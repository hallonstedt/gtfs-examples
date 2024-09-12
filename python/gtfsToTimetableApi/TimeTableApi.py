"""
A JSON HTTP API for realtime timetables. Requires a static GTFS feed, VehiclePositions.pb and TripUpdates.pb.

"""
import json
import logging
import sys
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

	# Call the static time-table fetcher on every call to ensure that it updates every 24h
	gtfs_path = GtfsArchiveFetcher.fetch_and_extract(config['URL']['gtfs'], "gtfs/")
	# The TimeTableQueryEngine class does not update the window dynamically so we send updated start and stop times from this call
	window_start = datetime.now() - timedelta(minutes=10)
	window_end = datetime.now() + timedelta(hours=config.getint('DEFAULT', 'window_size_hours'))
	resp = flask.Response(json.dumps(query_engine.create_departures_timetable(stop_id, window_start, window_end)))
	resp.headers['Content-encoding'] = 'UTF-8'
	resp.headers['Content-type'] = 'Application/json'
	return resp

@app.route('/stops/', methods=['GET'])
def stops():
	gtfs_path = GtfsArchiveFetcher.fetch_and_extract(config['URL']['gtfs'], "gtfs/")
	resp = flask.Response(json.dumps(query_engine.list_queryable_stops()))
	resp.headers['Content-encoding'] = 'UTF-8'
	resp.headers['Content-type'] = 'Application/json'
	return resp

@app.errorhandler(ValueError)
def handle_value_error(e):
    # Return the error message (not the full traceback)
    return f"Error: {str(e)}", 500

@app.errorhandler(500)
def handle_500_error(e):
	# Log the full error on the server
	app.logger.error(f"Server error: {e}")
	# Show the customized error message to the user
	return f"An internal error occurred: {e}", 500

realtime_data_fetcher = RealtimeDataFetcher(config['URL']['trip_updates'], config['URL']['vehicle_positions'])
# The Archive fetcher will only fetch a new file when needed
gtfs_path = GtfsArchiveFetcher.fetch_and_extract(config['URL']['gtfs'], "gtfs/")
query_engine = TimeTableQueryEngine(gtfs_path, realtime_data_fetcher, reduce_memory_usage=config.getboolean('Webserver', 'uncached'))

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
