"""Explicit configuration has no workstation identities or transport defaults."""

from dataclasses import asdict
import unittest
from unittest.mock import patch

from research_hub.renewal import Target, copy_guard, renewal_guard


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.mapping = dict(
            alias="task_123456789abc",
            workspace="fixture-space",
            owner_prefix="fixture-owner",
            uid_sha256="a" * 64,
            owner_user_id_sha256="b" * 64,
            profile_sha256="c" * 64,
            name_prefix="fixture-renew",
            display_name_prefix="Fixture",
        )

    def test_explicit_config_round_trip(self):
        with patch("research_hub.renewal.target.read_config", return_value=self.mapping) as read:
            self.assertEqual(asdict(Target.from_config("fixture-target.json")), self.mapping)
        read.assert_called_once_with("fixture-target.json")

    def test_incomplete_or_malformed_target_fails(self):
        for field in self.mapping:
            with self.subTest(missing=field):
                changed = dict(self.mapping)
                changed.pop(field)
                with self.assertRaises(ValueError):
                    Target.from_mapping(changed)
        for field, value in [
            ("alias", "any-task"),
            ("uid_sha256", "not-a-hash"),
            ("workspace", ""),
            ("name_prefix", "../outside"),
        ]:
            with self.subTest(invalid=field):
                changed = dict(self.mapping, **{field: value})
                with self.assertRaises(ValueError):
                    Target.from_mapping(changed)

    def test_no_implicit_live_transport(self):
        for module in (copy_guard, renewal_guard):
            with self.subTest(module=module.__name__):
                with self.assertRaises(TypeError):
                    module.Guard(
                        "policy.json", "cache.json", "sco", target=Target.from_mapping(self.mapping)
                    )


if __name__ == "__main__":
    unittest.main()
