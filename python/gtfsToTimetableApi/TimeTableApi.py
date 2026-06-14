"""
A JSON HTTP API for realtime timetables. Requires a static GTFS feed, VehiclePositions.pb and TripUpdates.pb.

"""
import json
import logging
import os
import sys
import threading
import flask
from datetime import datetime, timedelta

import gtfsparse

config = gtfsparse.validate_config('gtfs.conf')

# Initialize the logger before importing our other module. This way we see the output for the other module as well.
loglevel_constant = getattr(logging, config['DEFAULT']['log_level'].upper(), logging.INFO) # Convert the log level string to the corresponding logging level
root = logging.getLogger()
root.setLevel(loglevel_constant)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(loglevel_constant)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

logging.info('Starting JSON HTTP API based on GTFS and GTFS-RT data')
from GtfsTimeTable import TimeTableQueryEngine, GtfsArchiveFetcher
from RealtimeDataFetcher import RealtimeDataFetcher

app = flask.Flask(__name__)


query_engine = None
query_engine_gtfs_fingerprint = None
query_engine_lock = threading.RLock()


def get_gtfs_fingerprint(gtfs_path):
	feed_info_path = os.path.join(gtfs_path, "feed_info.txt")
	feed_info_stat = os.stat(feed_info_path)
	return (gtfs_path, feed_info_stat.st_mtime_ns, feed_info_stat.st_size)


def reload_query_engine(gtfs_path):
	global query_engine, query_engine_gtfs_fingerprint
	query_engine = TimeTableQueryEngine(
		gtfs_path,
		realtime_data_fetcher,
		reduce_memory_usage=config.getboolean('Webserver', 'uncached')
	)
	query_engine_gtfs_fingerprint = get_gtfs_fingerprint(gtfs_path)
	logging.info(f"Loaded GTFS query engine from {gtfs_path} with fingerprint {query_engine_gtfs_fingerprint}")
	return query_engine


def get_query_engine():
	global query_engine_gtfs_fingerprint
	with query_engine_lock:
		gtfs_path = GtfsArchiveFetcher.fetch_and_extract(config['URL']['gtfs'], "gtfs/")
		gtfs_fingerprint = get_gtfs_fingerprint(gtfs_path)
		if query_engine is None:
			return reload_query_engine(gtfs_path)
		if gtfs_fingerprint != query_engine_gtfs_fingerprint:
			logging.info(
				f"GTFS archive changed from {query_engine_gtfs_fingerprint} to {gtfs_fingerprint}; reloading query engine"
			)
			return reload_query_engine(gtfs_path)
		return query_engine


# Create the webserver and define actions for /departures and /stops
@app.route('/departures/', defaults={'stop_id': None}, methods=['GET'])  # This route is invoked if we pre-configured a stop in gtfs.conf
@app.route('/departures/<stop_id>', methods=['GET'])  # This route will be used if we over-ride defsult stop id and add one to the URL
def departures(stop_id):
	# Use default_stop_id if stop_id is not provided
	if stop_id is None:
		if config['DEFAULT']['stop_id'] == 'None':
			raise ValueError("A stop ID needs to be provided in the URL or configured in gtfs.conf<br>Locate the available stop IDs by requesting [server_ip:port]/stops/")
		else:
			stop_id = config['DEFAULT']['stop_id']

	current_query_engine = get_query_engine()
	# The TimeTableQueryEngine class does not update the window dynamically so we send updated start and stop times from this call
	window_start = datetime.now() - timedelta(minutes=10)
	window_end = datetime.now() + timedelta(hours=config.getint('DEFAULT', 'window_size_hours'))
	destination_stop_id = flask.request.args.get('destination_stop_id')
	resp = flask.Response(json.dumps(current_query_engine.create_departures_timetable(
		stop_id,
		window_start,
		window_end,
		destination_stop_id=destination_stop_id
	)))
	resp.headers['Content-encoding'] = 'UTF-8'
	resp.headers['Content-type'] = 'Application/json'
	return resp

@app.route('/stops/', methods=['GET'])
def stops():
	current_query_engine = get_query_engine()
	resp = flask.Response(json.dumps(current_query_engine.list_queryable_stops()))
	resp.headers['Content-encoding'] = 'UTF-8'
	resp.headers['Content-type'] = 'Application/json'
	return resp

@app.errorhandler(ValueError)
def handle_value_error(e):
    # Return the error message (not the full traceback)
    return f"Error: {str(e)}", 400

@app.errorhandler(500)
def handle_500_error(e):
	# Log the full error on the server
	app.logger.error(f"Server error: {e}")
	# Show the customized error message to the user
	return f"An internal error occurred: {e}", 500

realtime_data_fetcher = RealtimeDataFetcher(config['URL']['trip_updates'], config['URL']['vehicle_positions'])
# The Archive fetcher will only fetch a new file when needed
gtfs_path = GtfsArchiveFetcher.fetch_and_extract(config['URL']['gtfs'], "gtfs/")
reload_query_engine(gtfs_path)

if not config['DEFAULT']['log_level'].upper() == "DEBUG":
    app.config["DEBUG"] = False
else:
	app.config["DEBUG"] = True

webserver_port = config.getint('Webserver', 'ip_port')
if config.getboolean('Webserver', 'local_only'):
	logging.info(f"Starting webserver on localhost:{webserver_port}")
	app.run(port=webserver_port) # Bind only to localhost
else:
	logging.info(f"Starting webserver bound to all interfaces on port {webserver_port}")
	app.run(host="0.0.0.0", port=webserver_port) # Bind to all interfaces
