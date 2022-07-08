import logging
from typing import List, Optional, Tuple
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.appservice import ApplicationService
from synapse.rest import admin
from synapse.rest.client import login, register, room, room_batch, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomStreamToken
from synapse.util import Clock

from tests import unittest

logger = logging.getLogger(__name__)


def _create_join_state_events_for_batch_send_request(
    virtual_user_ids: List[str],
    insert_time: int,
) -> List[JsonDict]:
    return [
        {
            "type": EventTypes.Member,
            "sender": virtual_user_id,
            "origin_server_ts": insert_time,
            "content": {
                "membership": "join",
                "displayname": "display-name-for-%s" % (virtual_user_id,),
            },
            "state_key": virtual_user_id,
        }
        for virtual_user_id in virtual_user_ids
    ]


def _create_message_events_for_batch_send_request(
    virtual_user_id: str, insert_time: int, count: int
) -> List[JsonDict]:
    return [
        {
            "type": EventTypes.Message,
            "sender": virtual_user_id,
            "origin_server_ts": insert_time,
            "content": {
                "msgtype": "m.text",
                "body": "Historical %d" % (i),
                EventContentFields.MSC2716_HISTORICAL: True,
            },
        }
        for i in range(count)
    ]


class RoomBatchTestCase(unittest.HomeserverTestCase):
    """Test importing batches of historical messages."""

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room_batch.register_servlets,
        room.register_servlets,
        register.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

        self.appservice = ApplicationService(
            token="i_am_an_app_service",
            id="1234",
            namespaces={"users": [{"regex": r"@as_user.*", "exclusive": True}]},
            # Note: this user does not have to match the regex above
            sender="@as_main:test",
        )

        mock_load_appservices = Mock(return_value=[self.appservice])
        with patch(
            "synapse.storage.databases.main.appservice.load_appservices",
            mock_load_appservices,
        ):
            hs = self.setup_test_homeserver(config=config)
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.clock = clock
        self._storage_controllers = hs.get_storage_controllers()

        self.room_creator_user_id, _ = self.register_appservice_user(
            "as_user_creator", self.appservice.token
        )
        self.virtual_user_id, _ = self.register_appservice_user(
            "as_user_potato", self.appservice.token
        )

    def _create_test_room(
        self, room_version: RoomVersion = RoomVersions.V9
    ) -> Tuple[str, str, str, str]:
        room_id = self.helper.create_room_as(
            self.appservice.sender,
            room_version=room_version.identifier,
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )

        res_a = self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "A"},
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )
        event_id_a = res_a["event_id"]

        res_b = self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "B"},
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )
        event_id_b = res_b["event_id"]

        res_c = self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "C"},
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )
        event_id_c = res_c["event_id"]

        return room_id, event_id_a, event_id_b, event_id_c

    def _create_batch_send_url(
        self, room_id: str, prev_event_id: str, user_id: Optional[str] = None
    ) -> str:
        params = {
            "prev_event_id": prev_event_id,
            "user_id": user_id or self.room_creator_user_id,
        }
        return (
            "/_matrix/client/unstable/org.matrix.msc2716/rooms/{}/batch_send?{}".format(
                room_id, urlencode(params)
            )
        )

    @unittest.override_config({"experimental_features": {"msc2716_enabled": True}})
    def test_same_state_groups_for_whole_historical_batch(self) -> None:
        """Make sure that when using the `/batch_send` endpoint to import a
        bunch of historical messages, it re-uses the same `state_group` across
        the whole batch. This is an easy optimization to make sure we're getting
        right because the state for the whole batch is contained in
        `state_events_at_start` and can be shared across everything.
        """

        time_before_room = int(self.clock.time_msec())
        room_id, event_id_a, _, _ = self._create_test_room()

        channel = self.make_request(
            "POST",
            self._create_batch_send_url(room_id, event_id_a),
            content={
                "events": _create_message_events_for_batch_send_request(
                    self.virtual_user_id, time_before_room, 3
                ),
                "state_events_at_start": _create_join_state_events_for_batch_send_request(
                    [self.virtual_user_id], time_before_room
                ),
            },
            access_token=self.appservice.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Get the historical event IDs that we just imported
        historical_event_ids = channel.json_body["event_ids"]
        self.assertEqual(len(historical_event_ids), 3)

        # Fetch the state_groups
        state_group_map = self.get_success(
            self._storage_controllers.state.get_state_groups_ids(
                room_id, historical_event_ids
            )
        )

        # We expect all of the historical events to be using the same state_group
        # so there should only be a single state_group here!
        self.assertEqual(
            len(state_group_map.keys()),
            1,
            "Expected a single state_group to be returned by saw state_groups=%s"
            % (state_group_map.keys(),),
        )

    @unittest.override_config({"experimental_features": {"msc2716_enabled": True}})
    def test_sync_while_batch_importing(self) -> None:
        """
        Make sure that /sync correctly returns full room state when a user joins
        during ongoing batch backfilling.
        See: https://github.com/matrix-org/synapse/issues/12281
        """
        # Create user who will be invited & join room
        user_id = self.register_user("beep", "test")
        user_tok = self.login("beep", "test")

        time_before_room = int(self.clock.time_msec())

        # Create a room with some events
        room_id, _, _, _ = self._create_test_room()
        # Invite the user
        self.helper.invite(
            room_id,
            src=self.appservice.sender,
            targ=user_id,
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )

        # Create another room, send a bunch of events to advance the stream token
        other_room_id = self.helper.create_room_as(
            self.appservice.sender,
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )
        for _ in range(5):
            self.helper.send_event(
                room_id=other_room_id,
                type=EventTypes.Message,
                content={"msgtype": "m.text", "body": "C"},
                tok=self.appservice.token,
                appservice_user_id=self.room_creator_user_id,
            )

        # Join the room as the normal user
        self.helper.join(room_id, user_id, tok=user_tok)

        # Create an event to hang the historical batch from - In order to see
        # the failure case originally reported in #12281, the historical batch
        # must be hung from the most recent event in the room so the base
        # insertion event ends up with the highest `topogological_ordering`
        # (`depth`) in the room but will have a negative `stream_ordering`
        # because it's a `historical` event. Previously, when assembling the
        # `state` for the `/sync` response, the bugged logic would sort by
        # `topological_ordering` descending and pick up the base insertion
        # event because it has a negative `stream_ordering` below the given
        # pagination token. Now we properly sort by `stream_ordering`
        # descending which puts `historical` events with a negative
        # `stream_ordering` way at the bottom and aren't selected as expected.
        response = self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "C"},
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )
        event_to_hang_id = response["event_id"]

        channel = self.make_request(
            "POST",
            self._create_batch_send_url(room_id, event_to_hang_id),
            content={
                "events": _create_message_events_for_batch_send_request(
                    self.virtual_user_id, time_before_room, 3
                ),
                "state_events_at_start": _create_join_state_events_for_batch_send_request(
                    [self.virtual_user_id], time_before_room
                ),
            },
            access_token=self.appservice.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Now we need to find the invite + join events stream tokens so we can sync between
        main_store = self.hs.get_datastores().main
        events, next_key = self.get_success(
            main_store.get_recent_events_for_room(
                room_id,
                50,
                end_token=main_store.get_room_max_token(),
            ),
        )
        invite_event_position = None
        for event in events:
            if (
                event.type == "m.room.member"
                and event.content["membership"] == "invite"
            ):
                invite_event_position = self.get_success(
                    main_store.get_topological_token_for_event(event.event_id)
                )
                break

        assert invite_event_position is not None, "No invite event found"

        # Remove the topological order from the token by re-creating w/stream only
        invite_event_position = RoomStreamToken(None, invite_event_position.stream)

        # Sync everything after this token
        since_token = self.get_success(invite_event_position.to_string(main_store))
        sync_response = self.make_request(
            "GET",
            f"/sync?since={since_token}",
            access_token=user_tok,
        )

        # Assert that, for this room, the user was considered to have joined and thus
        # receives the full state history
        state_event_types = [
            event["type"]
            for event in sync_response.json_body["rooms"]["join"][room_id]["state"][
                "events"
            ]
        ]

        assert (
            "m.room.create" in state_event_types
        ), "Missing room full state in sync response"

    @unittest.override_config({"experimental_features": {"msc2716_enabled": True}})
    def test_room_batch_id_persistence(self) -> None:
        """Make sure that the next_batch_id sent back to the user is persisted
        in the database.
        """

        time_before_room = int(self.clock.time_msec())
        room_id, event_id_a, _, _ = self._create_test_room()

        channel = self.make_request(
            "POST",
            self._create_batch_send_url(room_id, event_id_a),
            content={
                "events": _create_message_events_for_batch_send_request(
                    self.virtual_user_id, time_before_room, 3
                ),
                "state_events_at_start": _create_join_state_events_for_batch_send_request(
                    [self.virtual_user_id], time_before_room
                ),
            },
            access_token=self.appservice.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        corresponding_insertion_event_id = self.get_success(
            self._storage_controllers.main.get_insertion_event_id_by_batch_id(
                room_id, channel.json_body["next_batch_id"]
            )
        )

        # The insertion event ID from the database should be the same as the
        # insertion event ID from the batch response.
        self.assertEqual(
            corresponding_insertion_event_id,
            channel.json_body["insertion_event_id"],
        )

    @unittest.override_config({"experimental_features": {"msc2716_enabled": True}})
    def test_batch_send_as_non_creator_v9(self) -> None:
        """Make sure that the batch send fails with 403 FORBIDDEN when the user
        making the /batch_send request is not the creator of the room.
        """

        time_before_room = int(self.clock.time_msec())
        room_id, event_id_a, _, _ = self._create_test_room()

        channel = self.make_request(
            "POST",
            self._create_batch_send_url(
                room_id, event_id_a, user_id=self.virtual_user_id
            ),
            content={
                "events": _create_message_events_for_batch_send_request(
                    self.virtual_user_id, time_before_room, 3
                ),
                "state_events_at_start": _create_join_state_events_for_batch_send_request(
                    [self.virtual_user_id], time_before_room
                ),
            },
            access_token=self.appservice.token,
        )
        self.assertEqual(channel.code, 403, channel.result)
        self.assertEqual(
            channel.json_body["error"],
            "In room version 9, only the room creator can use the /batch_send endpoint",
            channel.json_body,
        )

    @unittest.override_config({"experimental_features": {"msc2716_enabled": True}})
    def test_batch_send_without_permission_msc2716_room_version(self) -> None:
        """Make sure that the batch send fails when the user does not have the
        ``historical`` power level in a room version with MSC2716 enabled.
        """

        time_before_room = int(self.clock.time_msec())
        room_id, event_id_a, _, _ = self._create_test_room(
            room_version=RoomVersions.MSC2716v3
        )

        channel = self.make_request(
            "POST",
            self._create_batch_send_url(
                room_id, event_id_a, user_id=self.virtual_user_id
            ),
            content={
                "events": _create_message_events_for_batch_send_request(
                    self.virtual_user_id, time_before_room, 3
                ),
                "state_events_at_start": _create_join_state_events_for_batch_send_request(
                    [self.virtual_user_id], time_before_room
                ),
            },
            access_token=self.appservice.token,
        )
        self.assertEqual(channel.code, 403, channel.result)
        self.assertEqual(
            channel.json_body["error"],
            "You don't have permission to send send historical related events "
            '("insertion", "batch", and "marker")',
            channel.json_body,
        )

    @unittest.override_config({"experimental_features": {"msc2716_enabled": True}})
    def test_batch_send_with_permission_msc2716_room_version(self) -> None:
        """Make sure that the batch send succeeds when the user does have the
        ``historical`` power level in a room version with MSC2716 enabled, even
        if they are not the creator of the room.
        """

        time_before_room = int(self.clock.time_msec())
        room_id, event_id_a, _, _ = self._create_test_room(
            room_version=RoomVersions.MSC2716v3
        )

        power_levels = self.helper.get_state(
            room_id,
            EventTypes.PowerLevels,
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )
        power_levels["historical"] = 0
        self.helper.send_state(
            room_id,
            EventTypes.PowerLevels,
            power_levels,
            tok=self.appservice.token,
            appservice_user_id=self.room_creator_user_id,
        )

        channel = self.make_request(
            "POST",
            self._create_batch_send_url(
                room_id, event_id_a, user_id=self.virtual_user_id
            ),
            content={
                "events": _create_message_events_for_batch_send_request(
                    self.virtual_user_id, time_before_room, 3
                ),
                "state_events_at_start": _create_join_state_events_for_batch_send_request(
                    [self.virtual_user_id], time_before_room
                ),
            },
            access_token=self.appservice.token,
        )
        self.assertEqual(channel.code, 200, channel.result)
