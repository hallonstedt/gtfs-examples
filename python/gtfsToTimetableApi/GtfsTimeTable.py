import datetime
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta
from io import BytesIO

import requests
import urllib3

from GtfsCacheHelpers import GtfsStopsCache, GtfsRoutesCache, GtfsTripsCache, GtfsStopTimesCache, GtfsCalendarDatesCache
from RealtimeDataFetcher import RealtimeDataFetcher

ROUTE_TYPE_NAMES = {
    100: "TRAIN",
    401: "METRO",
    700: "BUS",
    900: "TRAM",
    1000: "FERRY",
    1501: "NÄRTRAFIK",
}


class GtfsArchiveFetcher:
    """
    This is a helper class to download and extract GTFS archives.
    """

    @staticmethod
    def fetch_and_extract(url: str, directory: str) -> str:
        """
        Fetch a GTFS file if it hasn't been fetched recently, and extract it.
        :param url: The url to download the archive from in case this is needed.
        :param directory: Where to extract the archive to
        :return: The directory containing the extracted archive
        """
        filename = os.path.basename(urllib3.util.parse_url(url).path)[0:-4]  # Get the filename from the URL
        directory_path = os.path.join(os.getcwd(), directory, filename)

        if not os.path.exists(directory_path):
            os.makedirs(directory_path)

        # Check if a download is needed
        if not GtfsArchiveFetcher.archive_exists(directory_path) \
                or GtfsArchiveFetcher.is_archive_outdated(directory_path):
            logging.info("Updating GTFS archive")
            r = requests.get(url, allow_redirects=True)
            zipdata = BytesIO()
            zipdata.write(r.content)
            with zipfile.ZipFile(zipdata) as zip_ref:
                zip_ref.extractall(directory_path)
        return directory_path

    @staticmethod
    def archive_exists(directory):
        return os.path.exists(directory) \
               and os.path.isdir(directory) \
               and os.path.exists(os.path.join(directory, "feed_info.txt"))

    @staticmethod
    def is_archive_outdated(directory):
        """
        Determine if the GTFS feed in a directory is outdated based on the creation time of the feed_info.txt file.
        :param directory: The directory containing the extracted GTFS feed.
        :return: True if outdated, False otherwise.
        """
        modification_time_epoch = os.path.getmtime(os.path.join(directory, "feed_info.txt"))
        creation_time = datetime.fromtimestamp(modification_time_epoch)
        is_outdated = datetime.now() - creation_time > timedelta(days=1)
        if is_outdated:
            logging.info("GTFS archive is older than 1 day")
        else:
            logging.debug("GTFS archive is less than 1 day old") # Since we check this on every call loglevel is set to DEBUG when the archive is up to date
        return is_outdated


class TimeTableQueryEngine:

    def __init__(self, gtfs_root: str, realtime_fetcher: RealtimeDataFetcher, reduce_memory_usage: bool = False):
        logging.info("Initializing TimeTableQueryEngine")
        if reduce_memory_usage:
            logging.warning("Reduced memory usage is enabled."
                            " This will reduce memory usage by up to 90%, at the cost of slower queries.")
        # Initialize all caches here
        self._realtime_fetcher = realtime_fetcher
        self._gtfs_root = gtfs_root
        self._calendar_dates_cache = GtfsCalendarDatesCache(self._gtfs_root)
        self._stops_cache = GtfsStopsCache(self._gtfs_root)
        logging.debug("Initializing stop times cache, this can take a while...")
        self._stop_times_cache = GtfsStopTimesCache(self._gtfs_root, reduce_memory_usage=reduce_memory_usage)
        logging.debug("Initialized stop times cache")
        self._routes_cache = GtfsRoutesCache(self._gtfs_root)
        self._trips_cache = GtfsTripsCache(self._gtfs_root)
        logging.info("Initialized TimeTableQueryEngine")

    def list_queryable_stops(self):
        """
        Get a list of all stops a user would want to search for (stations only, no quays or entrances)
        :return: A list of all stops a user would want to search for.
        """
        return [self._gtfs_stop_to_api_stop(stop) for stop in self._stops_cache.get_all_stops() if
                stop['location_type'] == '1']

    def create_departures_timetable(self,
                                    query_stop_id: str,
                                    window_start: datetime = datetime.now() - timedelta(minutes=10),
                                    window_end: datetime = datetime.now() + timedelta(hours=2)) -> object:
        """
        Create a TimeTable with departure information for a given stop.
        :param query_stop_id:  The id of the stop to search for. All quays in this stop will be automatically included.
        :param window_start: The start date/time of the time window in which to search.
        :param window_end: The end date/time of the time window in which to search.
                           Must be within 24h after after window_start.
        :return: An object containing information about the stops for which the timetable was constructed,
                 and the departures in the requested time frame.
        """
        assert window_start < window_end
        assert (window_end - window_start) < timedelta(days=1)  # The max interval is one day
        # Get the queried stop ids (stopplace + platforms)
        query_stop_ids = self._get_queried_stop_ids(query_stop_id)
        # Get the stop times at these stops
        stop_times = self._get_stop_times_for_stops(query_stop_ids)
        #print(stop_times)
        # Only retain stop times in the time window
        stop_times_in_window = self._filter_stop_times_window(stop_times, window_start, window_end)
        # Sort the stop times
        # Only sort when we have filtered out the interesting ones, to prevent wasting time on unnecessary sorting
        sorted_stop_times_in_window = self._amend_and_sort_stop_times(stop_times_in_window, window_start, window_end)
        # Compile a response based on the stop times and query ids.
        #return self._compile_results(sorted_stop_times_in_window, query_stop_ids), stop_times, sorted_stop_times_in_window
        return self._compile_results(sorted_stop_times_in_window, query_stop_ids)

    def _amend_and_sort_stop_times(self, stop_times_in_window: list, window_start: datetime, window_end: datetime) -> list:
        """
        This method will add actual time to the list, changing stop_times past midnight from 24+ format to regular hourly times, i.e. 25:10:00 -> 01:10:00
        It will also sort stop_times based on whether the window spans across midnight or not.
        :param stop_times_in_window: The list of stop times to adjust and sort.
        :param window_start: The start of the time window.
        :param window_end: End of the time window
        :return: The sorted list with adjusted_departure_time and adjusted_departure_seconds added to it.
        """
        logging.debug("Sorting filtered stop times chronologically")
        window_start_secs = window_start.hour * 3600 + window_start.minute * 60 + window_start.second
        window_end_secs = window_end.hour * 3600 + window_end.minute * 60 + window_end.second

        amended_stop_times = []

        for stop_time in stop_times_in_window:
            departure_seconds = stop_time['departure_seconds']

            # Normalize times greater than 24:00:00 (e.g., 25:00:00 -> 01:00:00)
            if departure_seconds >= 86400:
                adjusted_departure_seconds = departure_seconds - 86400
                adjusted_departure_time = self._seconds_to_time_string(adjusted_departure_seconds)
                logging.debug(f"adjusted departure time is {adjusted_departure_time}")
            else:
                adjusted_departure_seconds = departure_seconds
                adjusted_departure_time = stop_time['departure_time']

            # If the window spans midnight, adjust the sorting order
            if window_start_secs > window_end_secs:  # Window spans midnight
                if adjusted_departure_seconds < window_start_secs:
                    # This ensures that times after midnight are considered as "next day" times
                    adjusted_departure_seconds += 86400  # Add 24 hours to push past midnight times later

            # Update the dictionary with the new departure time and seconds
            stop_time['adjusted_departure_seconds'] = adjusted_departure_seconds
            stop_time['adjusted_departure_time'] = adjusted_departure_time

            amended_stop_times.append(stop_time)

        # Sort stop times by the adjusted departure time
        sorted_stop_times = sorted(amended_stop_times, key=lambda x: x['adjusted_departure_seconds'])

        return sorted_stop_times

    def _get_queried_stop_ids(self, query_id: str) -> list:
        """
        Get the ids of the parent location and all quays, for the parent or quay provided.
        :param query_id:  Parent location or quay id
        :return:  The ids of the parent location and all quays
        """
        logging.debug("Getting related stops")
        stop = self._stops_cache.get_stop(query_id)
        if stop['parent_station']:
            # Ensure we always search from the top-level stop
            stop = self._stops_cache.get_stop(stop['parent_station'])
        return [stop['stop_id']] + \
               [stop['stop_id'] for stop in self._stops_cache.get_all_quays_in_stop_place(stop['stop_id'])]

    def _get_stop_times_for_stops(self, stop_ids):
        logging.debug("Getting stop times for stops")
        # Get a list of all the stop times at the given stop_ids
        return self._stop_times_cache.get_stop_times_for_stops(stop_ids)

    def _filter_stop_times_window(self, stop_times: list, window_start: datetime, window_end: datetime) -> list:
        """
        This method will only retain the stop times in the given time window
        :param stop_times: The list of stop times to filter.
        :param window_start: The start of the time window.
        :param window_end: End of the time window
        :return: The filtered list
        """
        logging.debug("Filtering stop times")

        # Check if the time window spans across midnight
        window_crosses_midnight = window_end.date() > window_start.date()

        # Calculate the seconds from midnight. This way we can do all later comparisons using integers
        window_start_secs_since_midnight = window_start.time().hour * 3600 \
                                           + window_start.time().minute * 60 \
                                           + window_start.time().second

        window_end_secs_since_midnight = window_end.time().hour * 3600 \
                                         + window_end.time().minute * 60 \
                                         + window_end.time().second
        logging.debug(f"Selected window starts at {window_start.strftime('%Y-%m-%d %H:%M')} which equals {window_start_secs_since_midnight} seconds since midnight and it ends at {window_end.strftime('%Y-%m-%d %H:%M')} which is {window_end_secs_since_midnight} seconds since midnight")
        if window_crosses_midnight:
            logging.debug("The selection Window crosses midnight so we will include stop_times after 00:00")

        # Get the day before the start date, needed to check if a trip that spans multiple days was active on this day.
        day_before_start = window_start.date() - timedelta(days=1)

        filtered_stop_times = []

        for stop_time in stop_times:
            secs_since_midnight = stop_time['departure_seconds']
            hour_int = secs_since_midnight // 3600

            # Normalize times greater than 24:00:00 (e.g., 25:25:00 -> 01:25:00)
            if secs_since_midnight >= 86400:
                adjusted_secs_since_midnight = secs_since_midnight - 86400
            else:
                adjusted_secs_since_midnight = secs_since_midnight

            # Check if the time is within the window
            if not self._is_time_in_window(adjusted_secs_since_midnight,
                                           window_start_secs_since_midnight,
                                           window_end_secs_since_midnight,
                                           window_crosses_midnight):
                continue

            # Handle service checks for both cases: before and after midnight
            trip = self._trips_cache.get_trip(stop_time['trip_id'])
            service_id = trip['service_id']

            # If the stop time is before midnight
            if hour_int < 24:
                if not window_crosses_midnight:
                    if self._calendar_dates_cache.is_serviced(service_id, window_start.date()):
                        filtered_stop_times.append(stop_time)
                elif window_crosses_midnight:
                    if adjusted_secs_since_midnight >= window_start_secs_since_midnight:
                        if self._calendar_dates_cache.is_serviced(service_id, window_start.date()):
                            filtered_stop_times.append(stop_time)
                    else:
                        if self._calendar_dates_cache.is_serviced(service_id, window_end.date()):
                            filtered_stop_times.append(stop_time)
            # If the stop time is after midnight (adjusted from > 24:00:00)
            elif hour_int >= 24:
                if not window_crosses_midnight and self._calendar_dates_cache.is_serviced(service_id, day_before_start):
                    filtered_stop_times.append(stop_time)
                elif window_crosses_midnight:
                    if adjusted_secs_since_midnight >= window_start_secs_since_midnight:
                        if self._calendar_dates_cache.is_serviced(service_id, day_before_start):
                            filtered_stop_times.append(stop_time)
                    else:
                        if self._calendar_dates_cache.is_serviced(service_id, window_start.date()):
                            filtered_stop_times.append(stop_time)

        return filtered_stop_times

    def _is_time_in_window(self,
                           seconds_since_midnight: int,
                           window_start_since_midnight: int,
                           window_end_since_midnight: int,
                           window_crosses_midnight: bool # MH
                           ) -> bool:
        """
        Check if a time (in seconds from midnight) lies in a window. window_end can lie before window_start if
        window_end is on the next day. This only works with the 24h constraint on the time window.
        :param seconds_since_midnight: Time to check in seconds from midnight
        :param window_start_since_midnight: Start of the window
        :param window_end_since_midnight:  End of the window, less than 24h after the start. Can be smaller than
                                           window_start_since_midnight if it is a time during the next day.
        :param window_crosses_midnight: Helper boolean to determine if we need to deal with sop_times past midnight
        :return: True if the timestamp lies in the window.
        """
        if not window_crosses_midnight: # MH
            return window_start_since_midnight <= seconds_since_midnight < window_end_since_midnight # MH
        else: # MH
            return seconds_since_midnight >= window_start_since_midnight or seconds_since_midnight < window_end_since_midnight # MH


    def _compile_results(self, stop_times: list, searched_stop_ids: list) -> object:
        """
        Inflate a list of stop times (which are already filtered on location and time) to an API response.
        :param stop_times:  The stop times to include in the API response.
        :param searched_stop_ids:  The stop ids for which departures were calculated.
        :return: The API response
        """
        logging.debug("Compiling results")
        entries = list()
        for stop_time in stop_times:
            # Get additional information for each stop
            trip = self._trips_cache.get_trip(stop_time['trip_id'])
            route = self._routes_cache.get_route(trip['route_id'])
            stop = self._gtfs_stop_id_to_api_stop(stop_time['stop_id'])
            # Get realtime information
            delay = self._realtime_fetcher.get_delay_for_trip_stop(trip['trip_id'], stop_time['stop_sequence'])
            position = self._realtime_fetcher.get_position_for_trip(trip['trip_id'])
            occupancy = self._realtime_fetcher.get_occupancy_for_trip(trip['trip_id'])

            entries.append({
                "direction": stop_time['stop_headsign'],
                "scheduled_departure_time": stop_time['departure_time'],
                "realtime_departure_time": self._add_seconds(stop_time['adjusted_departure_time'], delay),
                "stop": stop,
                "type": ROUTE_TYPE_NAMES[int(route['route_type'])],
                "route_long": route['route_long_name'],
                "route_short": route['route_short_name'],
                "delay": delay,
                "position": position,
                "occupancy": occupancy
            })

        # Wrap departures and stops in one object
        return {"stops": [self._gtfs_stop_id_to_api_stop(stop_id) for stop_id in searched_stop_ids],
                "departures": entries}

    def _gtfs_stop_id_to_api_stop(self, stop_id):
        # This is just a helper metod to wrap the get_stop method call
        return self._gtfs_stop_to_api_stop(self._stops_cache.get_stop(stop_id))

    def _gtfs_stop_to_api_stop(self, stop):
        """
        Create an API stop object based on a GTFS stop. Some attributes are renamed, irrelevant data is left out.
        :param stop:  The GTFS stop.
        :return: The API stop.
        """
        return {
            "id": stop["stop_id"],
            "name": stop["stop_name"],
            "platform": stop["platform_code"],
            "latitude": stop["stop_lat"],
            "longitude": stop["stop_lon"],
        }

    def _add_seconds(self, time: str, seconds: int) -> str:
        """
        Add seconds to a time string
        :param time: A time in hh:mm:ss
        :param seconds: Seconds to add (or subtract) to the given time
        :return: The time with seconds added in hh:mm:ss format
        """
        if seconds == 0:
            return time
        time_seconds = self.get_seconds_since_midnight(time)
        if time_seconds < 0:
            time_seconds += 24 * 3600  # + 1 day in case the delay was negative and we got below zero
        time_seconds += seconds
        # Seconds to hh:mm:ss
        m, s = divmod(time_seconds, 60)  # Get quotient and modulo in one operation
        h, m = divmod(m, 60)
        return f'{h:02d}:{m:02d}:{s:02d}'

    def get_seconds_since_midnight(self, time_str: str) -> int:
        """Get Seconds from time, for faster calculations later on."""
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + int(s)

    def _seconds_to_time_string(self, seconds: int) -> str:
        """
        Helper function to convert seconds since midnight to hh:mm:ss format
        :param seconds: Seconds to add (or subtract) to the given time
        :return: The time with seconds added in hh:mm:ss format
        """
        m, s = divmod(seconds, 60)  # Get quotient and modulo in one operation
        h, m = divmod(m, 60)
        return f'{h:02d}:{m:02d}:{s:02d}'


if __name__ == '__main__':
    import gtfsparse
    import json

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

    logging.info("CLI script to get a realtime timetable for a stop based on GTFS and GTFS-RT data")

    realtime_data_fetcher = RealtimeDataFetcher(config['URL']['trip_updates'], config['URL']['vehicle_positions'])
    # The Archive fetcher will only fetch a new file when needed
    gtfs_path = GtfsArchiveFetcher.fetch_and_extract(config['URL']['gtfs'], "gtfs/")
    # The query engine will calculate most of the data on-the-fly.
    # Only one query will be made, so favor lower memory usage since the longer query time
    # will be offset by the reduced startup time.
    query_engine = TimeTableQueryEngine(gtfs_path, realtime_data_fetcher, reduce_memory_usage=True)
    # Run a sample query and print the result

    default_stop_id = config['DEFAULT'].get('stop_id')
    if default_stop_id == 'None':
        default_stop_id = False
    # Show the default stop_id (if available) to the user and prompt for input
    if default_stop_id:
        stop_id = input(f"Enter stop_id (or press Enter to use the default: {default_stop_id}): ")
    else:
        stop_id = input("Enter stop ID: ")
        if stop_id == '':
            stop_id = False
    # If the user presses Enter without typing anything, use the default_stop_id
    if not stop_id and default_stop_id:
        stop_id = default_stop_id
    if not stop_id and not default_stop_id:
        print("A stop ID is required for this program to work")
        exit(1)

    window_start = datetime.now() - timedelta(minutes=10)
    window_end = datetime.now() + timedelta(hours=config.getint('DEFAULT', 'window_size_hours'))
    result = query_engine.create_departures_timetable(stop_id, window_start, window_end)

    print(json.dumps(result, indent=4, ensure_ascii=False))
