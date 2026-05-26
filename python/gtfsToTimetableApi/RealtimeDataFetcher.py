import time
import logging

import requests
from google.transit import gtfs_realtime_pb2
from google.protobuf.message import DecodeError

# This map describes the GTFS Occupancy enum, and is used to convert numeric values to their string representation.
OCCUPANCY_STATUS_MAP = ["EMPTY",
                        "MANY_SEATS_AVAILABLE",
                        "FEW_SEATS_AVAILABLE",
                        "STANDING_ROOM_ONLY",
                        "CRUSHED_STANDING_ROOM_ONLY",
                        "FULL",
                        "NOT_ACCEPTING_PASSENGERS"]


class RealtimeDataFetcher:
    """
    This class fetches TripUpdates.pb and VehicleUpdates.pb files, and converts them into dictionaries. Data is cached
    and refreshed when needed.
    """
    def __init__(self, tripupdates_url, vehicle_positions_url):
        self._tripupdates_url = tripupdates_url
        self._positions_url = vehicle_positions_url
        self._delays = dict()
        self._delays_last_updated = None
        self._positions = dict()
        self._occupancies = dict()
        self._positions_last_updated = None

    def get_delays(self):
        """
        Get the data. Cached data if it was fetched recently,
        or a fresh copy if the stored data is expired.
        :return: a dict mapping (trip id, stop sequence) tuples to the delay for that departure.
        """
        if self._are_delays_outdated():
            self._refresh_delays()
        return self._delays

    def get_positions(self):
        """
        Get the data. Cached data if it was fetched recently,
        or a fresh copy if the stored data is expired.
        :return: a dict mapping trip_ids to vehicle positions.
        """
        if self._are_positions_outdated():
            self._refresh_vehicle_position_data()
        return self._positions

    def get_occupancies(self):
        """
        Get the data. Cached data if it was fetched recently,
        or a fresh copy if the stored data is expired.
        :return: a dict mapping trip_ids to occupancies positions.
        """
        if self._are_positions_outdated():
            # Occupancies are obtained from vehiclepositions.txt
            self._refresh_vehicle_position_data()
        return self._occupancies

    def _are_delays_outdated(self):
        now = int(time.time())
        # Data never fetched or data older than x seconds
        return self._delays_last_updated is None or now - self._delays_last_updated > 60

    def _are_positions_outdated(self):
        now = int(time.time())
        # Data never fetched or data older than x seconds
        return self._positions_last_updated is None or now - self._positions_last_updated > 15

    def _refresh_delays(self):
        delays = dict()
        feed = self._fetch_feed(self._tripupdates_url, "TripUpdates")
        if feed is None:
            # Keep previous data, but mark as updated to avoid hammering the endpoint on every API request.
            self._delays_last_updated = int(time.time())
            return
        for entity in feed.entity:
            if entity.HasField('trip_update'):
                # Handle
                trip_id = entity.trip_update.trip.trip_id
                for update in entity.trip_update.stop_time_update:
                    stop_sequence = update.stop_sequence  # ambiguous, need stop_sequence!
                    if not update.HasField('departure'):
                        continue
                    delay = update.departure.delay
                    delays[(trip_id, stop_sequence)] = delay
        self._delays = delays
        self._delays_last_updated = int(time.time())

    def _refresh_vehicle_position_data(self):
        positions = dict()
        occupancies = dict()
        feed = self._fetch_feed(self._positions_url, "VehiclePositions")
        if feed is None:
            # Keep previous data, but mark as updated to avoid hammering the endpoint on every API request.
            self._positions_last_updated = int(time.time())
            return
        for entity in feed.entity:
            if entity.HasField('vehicle'):
                # Handle
                position = entity.vehicle.position
                trip_id = entity.vehicle.trip.trip_id
                positions[trip_id] = {
                    "latitude": position.latitude,
                    "longitude": position.longitude,
                    "bearing": position.bearing,
                    "speed": position.speed * 3.6,  # m/s to kph
                }
                occupancies[trip_id] = entity.vehicle.occupancy_status
        self._occupancies = occupancies
        self._positions = positions
        self._positions_last_updated = int(time.time())

    def get_delay_for_trip_stop(self, trip_id, stop_sequence):
        data = self.get_delays()
        # Convert to int to match the data type from protobuf
        stop_sequence = int(stop_sequence)
        if (trip_id, stop_sequence) not in data:
            return 0
        return data[(trip_id, stop_sequence)]

    def get_position_for_trip(self, trip_id):
        data = self.get_positions()
        if trip_id not in data:
            return {}
        return data[trip_id]

    def get_occupancy_for_trip(self, trip_id):
        data = self.get_occupancies()
        if trip_id not in data:
            # If no data is present, or the value cannot be mapped
            return "UNKNOWN"
        occupancy_status = data[trip_id]
        if occupancy_status < 0 or occupancy_status >= len(OCCUPANCY_STATUS_MAP):
            return "UNKNOWN"
        return OCCUPANCY_STATUS_MAP[occupancy_status]

    def _fetch_feed(self, url: str, feed_name: str):
        """
        Fetch and parse a GTFS-RT protobuf feed.
        Returns None on fetch/parse errors so callers can fail soft.
        """
        try:
            response = requests.get(
                url,
                timeout=20,
                headers={"Accept-Encoding": "gzip, deflate"}
            )
        except requests.RequestException as exc:
            logging.error("Failed to fetch %s from %s: %s", feed_name, url, exc)
            return None

        content_type = (response.headers.get("content-type") or "").lower()
        if response.status_code != 200:
            preview = response.text[:200].replace("\n", " ")
            logging.error(
                "Failed to fetch %s from %s: HTTP %s, content-type=%s, body=%s",
                feed_name,
                url,
                response.status_code,
                content_type or "unknown",
                preview
            )
            return None

        if "json" in content_type or content_type.startswith("text/"):
            preview = response.text[:200].replace("\n", " ")
            logging.error(
                "Unexpected %s response format from %s: content-type=%s, body=%s",
                feed_name,
                url,
                content_type,
                preview
            )
            return None

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(response.content)
        except DecodeError as exc:
            preview = response.content[:200]
            text_preview = preview.decode("utf-8", errors="replace").replace("\n", " ")
            logging.error(
                "Failed to parse %s protobuf from %s: %s. First bytes (decoded)=%s",
                feed_name,
                url,
                exc,
                text_preview
            )
            return None
        return feed
