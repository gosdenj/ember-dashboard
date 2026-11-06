"""Tests for reading the real Hive payload.

The shape of the reply was confirmed against the live account on
2026-11-04. It is not the shape the old API used, and getting it wrong
produces an empty dashboard rather than an error - which is the worst
kind of wrong.

The shape:

  * top level is ``{products, devices, user, homes, actions, ...}``
  * a ``trvcontrol`` product is one **room**, with ``state.name`` for
    the name, ``props.temperature`` for what it is reading now and
    ``state.target`` for what it is being asked for
  * a ``heating`` product is the boiler controller
  * ``devices`` are the physical things; a zone's ``props.trvs`` lists
    the valves inside it
"""

import unittest

from ember.hive import Hive


def zone(name, measured, target, frost=7, mode="SCHEDULE", trvs=None,
         boost=None, zone_name="Hive Thermostat"):
    return {
        "type": "trvcontrol",
        "id": f"zone-{name}",
        "props": {
            "online": True,
            "temperature": measured,
            "previous": {"mode": mode, "target": target},
            "scheduleOverride": False,
            "zoneName": zone_name,
            "trvs": trvs if trvs is not None else [f"trv-{name}"],
        },
        "state": {
            "name": name,
            "mode": mode,
            "target": target,
            "frostProtection": frost,
            "boost": boost,
            "schedule": {"monday": []},
        },
    }


def heating_product(temperature=17.57, target=18, working="HEAT", boost=None):
    return {
        "type": "heating",
        "id": "heating-1",
        "props": {
            "online": True,
            "model": "SLR1",
            "modelVariant": "SLR1b",
            "temperature": temperature,
            "working": working,
            "holidayMode": {"active": False, "enabled": True,
                            "start": 0, "end": 0, "temperature": 7},
            "scheduleOverride": False,
            "previous": {"mode": "SCHEDULE"},
        },
        "state": {
            "name": "Hive Thermostat",
            "boost": boost,
            "frostProtection": 7,
            "mode": "SCHEDULE",
            "target": target,
            "optimumStart": False,
            "schedule": {"monday": []},
        },
    }


class FakeHive(Hive):
    """A Hive that answers with a payload instead of calling out."""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def nodes(self):
        return self.payload


class ReadStateTests(unittest.TestCase):
    def test_the_home_temperature_comes_from_the_heating_product(self):
        hive = FakeHive({"products": [heating_product(temperature=17.57)]})
        state = hive.read_state()
        self.assertAlmostEqual(state["home_temp"], 17.57)
        self.assertAlmostEqual(state["target_temp"], 18.0)

    def test_each_zone_becomes_a_room(self):
        payload = {"products": [
            heating_product(),
            zone("Dining Room", 17.4, 7, trvs=["trv-1"]),
            zone("Kitchen", 20.3, 20.0, trvs=["trv-2"]),
        ]}
        state = FakeHive(payload).read_state()
        rooms = {room["room"]: room for room in state["rooms"]}
        self.assertEqual(set(rooms), {"Dining Room", "Kitchen"})
        self.assertAlmostEqual(rooms["Dining Room"]["current"], 17.4)
        self.assertAlmostEqual(rooms["Dining Room"]["target"], 7.0)

    def test_a_room_below_its_target_is_calling_for_heat(self):
        """This is how the app decides ON versus OFF."""
        payload = {"products": [
            zone("Cold Room", 17.4, 19.5),
            zone("Warm Room", 20.3, 19.5),
        ]}
        rooms = {room["room"]: room for room in FakeHive(payload).read_state()["rooms"]}
        self.assertIs(rooms["Cold Room"]["calling"], True)
        self.assertIs(rooms["Warm Room"]["calling"], False)

    def test_a_room_at_frost_protection_is_not_calling(self):
        payload = {"products": [zone("Dining Room", 17.4, 7, frost=7)]}
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertIs(room["calling"], False)

    def test_the_boiler_state_comes_from_working(self):
        state = FakeHive({"products": [heating_product(working="HEAT")]}).read_state()
        self.assertIs(state["boiler_on"], True)
        state = FakeHive({"products": [heating_product(working="OFF")]}).read_state()
        self.assertIs(state["boiler_on"], False)

    def test_when_the_boiler_does_not_say_it_is_inferred(self):
        product = heating_product()
        del product["props"]["working"]
        payload = {"products": [product, zone("Cold Room", 17.0, 19.5)]}
        state = FakeHive(payload).read_state()
        self.assertIs(state["boiler_on"], True)

    def test_an_empty_payload_does_not_explode(self):
        state = FakeHive({}).read_state()
        self.assertEqual(state["rooms"], [])
        self.assertIsNone(state["home_temp"])

    def test_a_boost_is_reported(self):
        state = FakeHive({"products": [heating_product(boost="2026-11-04T21:45:00Z")]}).read_state()
        self.assertEqual(state["boost_until"], "2026-11-04T21:45:00Z")

    def test_a_valve_position_is_picked_up_from_the_device(self):
        payload = {
            "products": [zone("Dining Room", 17.4, 19.5, trvs=["trv-1"])],
            "devices": [{"id": "trv-1", "type": "trv",
                         "props": {"position": 75, "battery": 90}}],
        }
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertAlmostEqual(room["position"], 75)

    def test_devices_given_as_a_mapping_also_work(self):
        payload = {
            "products": [zone("Dining Room", 17.4, 19.5, trvs=["trv-1"])],
            "devices": {"trv-1": {"id": "trv-1", "type": "trv",
                                  "props": {"motorPosition": 40}}},
        }
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertAlmostEqual(room["position"], 40)

    def test_booleans_are_not_treated_as_numbers(self):
        payload = {"products": [zone("Dining Room", True, False)]}
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertIsNone(room["current"])
        self.assertIsNone(room["target"])

    def test_a_zone_with_two_valves_says_so(self):
        """Three of the nine rooms have a second radiator."""
        payload = {"products": [zone("Kitchen", 19.0, 7, trvs=["a", "b"])]}
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertEqual(room["valve_count"], 2)

    def test_a_zone_with_one_valve_says_so(self):
        payload = {"products": [zone("Kitchen", 19.0, 7, trvs=["a"])]}
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertEqual(room["valve_count"], 1)

    def test_the_lowest_battery_in_the_room_wins(self):
        payload = {
            "products": [zone("Kitchen", 19.0, 7, trvs=["a", "b"])],
            "devices": [
                {"id": "a", "type": "trv", "props": {"battery": 91}},
                {"id": "b", "type": "trv", "props": {"battery": 14}},
            ],
        }
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertAlmostEqual(room["battery"], 14)

    def test_a_valve_with_no_battery_reading_leaves_it_unknown(self):
        payload = {
            "products": [zone("Kitchen", 19.0, 7, trvs=["a"])],
            "devices": [{"id": "a", "type": "trv", "props": {}}],
        }
        room = FakeHive(payload).read_state()["rooms"][0]
        self.assertIsNone(room["battery"])

    def test_the_real_account_shape_parses(self):
        """A trimmed version of the reply from the live account.

        The real zones came back with a target of 7 for most rooms - that
        is the frost floor, not a target of 7 degrees, and it must not
        make the room look as though it is asking for heat.
        """
        payload = {
            "products": [
                {
                    "type": "heating",
                    "props": {"temperature": 17.55, "working": False,
                              "model": "SLR1", "online": True},
                    "state": {"target": 7, "frostProtection": 12, "boost": None,
                              "mode": "SCHEDULE", "autoBoost": "ENABLED",
                              "name": "Hive Thermostat"},
                },
                {
                    "type": "trvcontrol",
                    "props": {"temperature": 19.7, "online": True,
                              "trvs": ["81c1bcb6-c2cc-4805-aa70-6e679dacc2c2"]},
                    "state": {"name": "Sofia Bedroom", "target": 20,
                              "frostProtection": 7, "mode": "SCHEDULE",
                              "boost": None},
                },
                {
                    "type": "trvcontrol",
                    "props": {"temperature": 17.3, "online": True,
                              "trvs": ["d4f42d60-98f0-421d-83bf-5a2febffdcf5"]},
                    "state": {"name": "Dining Room", "target": 7,
                              "frostProtection": 7, "mode": "SCHEDULE",
                              "boost": None},
                },
            ],
            "devices": [
                {"id": "81c1bcb6-c2cc-4805-aa70-6e679dacc2c2", "type": "trv",
                 "props": {"battery": 100, "model": "TRV001", "online": True},
                 "state": {"name": "Sofia Bedroom"}},
                {"id": "d4f42d60-98f0-421d-83bf-5a2febffdcf5", "type": "trv",
                 "props": {"battery": 100, "model": "TRV001", "online": True},
                 "state": {"name": "Dining Room"}},
            ],
        }
        state = FakeHive(payload).read_state()
        rooms = {room["room"]: room for room in state["rooms"]}

        # Sofia Bedroom is below its target, so she is asking for heat.
        self.assertIs(rooms["Sofia Bedroom"]["calling"], True)
        # Dining Room's target is the frost floor. 17.3 is above 7, so
        # it is not asking for heat, even though the target looks low.
        self.assertIs(rooms["Dining Room"]["calling"], False)
        self.assertIs(state["boiler_on"], False)
        self.assertAlmostEqual(state["home_temp"], 17.55)


if __name__ == "__main__":
    unittest.main()
