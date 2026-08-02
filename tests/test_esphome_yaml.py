"""Tests for ESPHome device-YAML credential masking (esphome_yaml.py)."""

from __future__ import annotations

import pytest

from custom_components.phoenix_mcp.esphome_yaml import (
    EsphomeSecretViolation,
    esphome_rel_path_ok,
    inline_secret_values,
    redact_esphome_text,
    scrub_secret_values,
    secret_keys_from_text,
    secret_values_from_text,
    splice_esphome_text,
)
from custom_components.phoenix_mcp.yaml_includes import YamlParseError

# Mirrors the real shapes on a live box: inline api key, LIST-form ota (the
# modern syntax a legacy-only path list would miss), a !lambda C++ block, and
# wifi behind !secret.
LIVE_SHAPE = '''esphome:
  name: rf-blaster1

# Enable Home Assistant API
api:
  encryption:
    key: "ndqsa+Shak6xAu4IU1NW=="
  actions:
    - action: transmit_raw
      variables:
        timings: int[]
      then:
        - remote_transmitter.transmit_raw:
            code: !lambda |-
              esphome::remote_base::RawTimings v;
              return v;

ota:
  - platform: esphome
    password: "otapassword123"

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

uart:
  baud_rate: 9600
'''


class TestJail:
    @pytest.mark.parametrize("rel", [
        "rf-blaster1.yaml", "sub/dir/device.yml", "esphome-web-31fca4.yaml",
    ])
    def test_allowed(self, rel):
        assert esphome_rel_path_ok(rel) is True

    @pytest.mark.parametrize("rel", [
        "secrets.yaml", "SECRETS.YAML", "Secrets.Yaml",
        "archive/old.yaml", "ARCHIVE/old.yaml",
        ".device-builder.json", ".git/config", "sub/.hidden/x.yaml",
        "notes.txt", "device.yaml.bak", "", "   ",
    ])
    def test_refused(self, rel):
        assert esphome_rel_path_ok(rel) is False

    def test_non_string_refused(self):
        assert esphome_rel_path_ok(None) is False


class TestLayer1CuratedPaths:
    def test_api_key_and_list_form_ota_redacted(self):
        red, paths = redact_esphome_text(LIVE_SHAPE)
        assert paths == ["api.encryption.key", "ota[0].password"]
        assert "ndqsa" not in red
        assert "otapassword123" not in red
        assert "__PHOENIX_REDACTED__api.encryption.key__" in red
        # The legacy top-level ota.password path alone would have missed this.
        assert "__PHOENIX_REDACTED__ota[0].password__" in red

    def test_legacy_api_password_and_wifi_networks_list(self):
        text = (
            "api:\n  password: legacyapipw\n"
            "wifi:\n  networks:\n    - ssid: A\n      password: netpassword1\n"
            "    - ssid: B\n      password: netpassword2\n"
        )
        _, paths = redact_esphome_text(text)
        assert paths == ["api.password", "wifi.networks[0].password", "wifi.networks[1].password"]

    def test_tagged_scalars_and_comments_survive(self):
        red, _ = redact_esphome_text(LIVE_SHAPE)
        assert "ssid: !secret wifi_ssid" in red
        assert "password: !secret wifi_password" in red
        assert "!lambda" in red
        assert "# Enable Home Assistant API" in red
        assert "baud_rate: 9600" in red

    def test_unparseable_fails_closed(self):
        with pytest.raises(YamlParseError):
            redact_esphome_text("api:\n  key: [unclosed\n")

    def test_bare_api_and_passwordless_ota_walk_cleanly(self):
        # esphome-web-31fca4.yaml's real shape: no encryption block at all.
        text = "api:\n\n# Allow OTA\nota:\n- platform: esphome\n"
        red, paths = redact_esphome_text(text)
        assert paths == []
        assert red == text


class TestLayer2KeyNameHeuristic:
    def test_credential_key_at_an_uncurated_path(self):
        text = "ble_client:\n  - mac_address: AA\n    bindkey: abcdef0123456789\n"
        red, paths = redact_esphome_text(text)
        assert paths == ["ble_client[0].bindkey"]
        assert "abcdef0123456789" not in red

    def test_token_in_a_custom_component_block(self):
        text = "http_request:\n  headers:\n    auth_token: zzzsecrettokenzzz\n"
        _, paths = redact_esphome_text(text)
        assert paths == ["http_request.headers.auth_token"]

    def test_benign_keys_untouched(self):
        text = "sensor:\n  - platform: dht\n    model: DHT22\n    update_interval: 60s\n"
        red, paths = redact_esphome_text(text)
        assert paths == []
        assert red == text


class TestLayer3SecretsCrossCheck:
    def test_value_matching_secrets_yaml_at_an_unknown_path(self):
        # The real pattern: 9 files use !secret for wifi while 2 inline the same
        # value, so the inline copy must be caught even where the path is not a
        # known credential location.
        text = "substitutions:\n  wifi_pass: housepassword1\n"
        red, paths = redact_esphome_text(text, {"housepassword1"})
        assert paths == ["substitutions.wifi_pass"]
        assert "housepassword1" not in red

    def test_short_secret_values_never_trigger(self):
        # A short secrets.yaml value would otherwise mangle pins and intervals.
        text = "uart:\n  tx_pin: GPIO19\n  baud_rate: 9600\n"
        red, paths = redact_esphome_text(text, {"9600", "GPIO19"})
        assert paths == []
        assert red == text

    def test_substring_occurrences_never_trigger(self):
        text = "esphome:\n  name: housepassword1-extended-name\n"
        _, paths = redact_esphome_text(text, {"housepassword1"})
        assert paths == []

    def test_missing_secrets_degrades_to_layers_1_and_2(self):
        red, paths = redact_esphome_text(LIVE_SHAPE, set())
        assert paths == ["api.encryption.key", "ota[0].password"]
        assert "ndqsa" not in red

    def test_secret_parsing_helpers_tolerate_junk(self):
        assert secret_values_from_text("not: [valid") == set()
        assert secret_keys_from_text("not: [valid") == set()
        assert secret_values_from_text("a: short\nb: longenoughvalue\n") == {"longenoughvalue"}
        assert secret_keys_from_text("wifi_ssid: x\nwifi_password: y\n") == {"wifi_ssid", "wifi_password"}


class TestSpliceRoundTrip:
    def test_non_secret_edit_leaves_credentials_byte_identical(self):
        red, _ = redact_esphome_text(LIVE_SHAPE)
        edited = red.replace("baud_rate: 9600", "baud_rate: 115200")
        out = splice_esphome_text(edited, LIVE_SHAPE, set(), {"wifi_ssid", "wifi_password"})
        assert 'key: "ndqsa+Shak6xAu4IU1NW=="' in out
        assert 'password: "otapassword123"' in out
        assert "baud_rate: 115200" in out
        assert "# Enable Home Assistant API" in out
        assert "__PHOENIX_REDACTED__" not in out

    def test_placeholder_outside_a_frozen_span_stays_literal(self):
        # Parking a placeholder in a non-credential field must not splice a
        # credential into it.
        new = 'esphome:\n  name: __PHOENIX_REDACTED__api.encryption.key__\napi:\n  password: x\n'
        disk = "esphome:\n  name: dev\napi:\n  password: realpassword1\n"
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(new, disk, set(), set())


class TestWriteFreeze:
    DISK = 'api:\n  encryption:\n    key: "REALKEY123456="\nwifi:\n  password: "housepassword1"\n  ssid: "MyNet"\n'

    def _red(self):
        return redact_esphome_text(self.DISK, {"housepassword1"})[0]

    def test_changing_a_masked_value_refused(self):
        new = self._red().replace("__PHOENIX_REDACTED__api.encryption.key__", '"NEWKEY99="')
        with pytest.raises(EsphomeSecretViolation) as exc:
            splice_esphome_text(new, self.DISK, {"housepassword1"}, {"wifi_password"})
        # The message must name the fix: an agent told only "no" retries variations.
        assert "!secret" in str(exc.value)
        assert "secrets.yaml" in str(exc.value)

    def test_out_of_file_state_consequence_is_stated(self):
        new = self._red().replace("__PHOENIX_REDACTED__api.encryption.key__", '"NEWKEY99="')
        with pytest.raises(EsphomeSecretViolation) as exc:
            splice_esphome_text(new, self.DISK, {"housepassword1"}, {"wifi_password"})
        # Freezing is not only about disclosure; a blind change can strand a device.
        assert "unreachable" in str(exc.value)

    def test_cross_path_placeholder_refused(self):
        new = self._red().replace(
            "key: __PHOENIX_REDACTED__api.encryption.key__",
            "key: __PHOENIX_REDACTED__wifi.password__")
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(new, self.DISK, {"housepassword1"}, {"wifi_password"})

    def test_new_inline_credential_refused(self):
        new = self._red().replace('ssid: "MyNet"', 'ssid: "MyNet"\n  ap:\n    password: "brandnew1"')
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(new, self.DISK, {"housepassword1"}, {"wifi_password"})

    def test_laundering_a_known_secret_elsewhere_refused(self):
        new = self._red().replace('ssid: "MyNet"', 'ssid: "housepassword1"')
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(new, self.DISK, {"housepassword1"}, {"wifi_password"})

    def test_placeholder_with_no_disk_counterpart_refused(self):
        new = 'api:\n  encryption:\n    key: __PHOENIX_REDACTED__api.encryption.key__\n'
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(new, "api:\n  encryption:\n    key: !secret k\n", set(), {"k"})

    def test_non_credential_edit_allowed(self):
        new = self._red().replace('ssid: "MyNet"', 'ssid: "OtherNet"')
        out = splice_esphome_text(new, self.DISK, {"housepassword1"}, {"wifi_password"})
        assert 'ssid: "OtherNet"' in out
        assert 'key: "REALKEY123456="' in out


class TestRestoreWaiver:
    """A version restore re-applies RAW text, which carries the file's inline
    credentials as literals. The three literal refusals read that as an agent
    authoring credentials, which would make any device file carrying an inline
    credential impossible to restore, and inline credentials are the normal case
    in these files. The waiver is scoped to restore, never to a caller.
    """

    DISK = 'api:\n  encryption:\n    key: "REALKEY123456="\nwifi:\n  password: "housepassword1"\n  ssid: "MyNet"\n'

    def test_the_exact_live_failure_rewriting_a_file_unchanged(self):
        # Byte-for-byte what is already on disk, which is what a restore does.
        with pytest.raises(EsphomeSecretViolation) as exc:
            splice_esphome_text(self.DISK, self.DISK, {"housepassword1"}, {"wifi_password"})
        assert "cannot be changed to a new value" in str(exc.value)

        out = splice_esphome_text(
            self.DISK, self.DISK, {"housepassword1"}, {"wifi_password"},
            allow_literal_credentials=True)
        assert out == self.DISK

    def test_rolling_back_to_a_snapshot_that_predates_a_credential(self):
        # The other real shape: the snapshot has no OTA password, disk now does.
        disk = 'ota:\n  - platform: esphome\n    password: "Kx9fQ2mVb7"\nwifi:\n  password: "housepassword1"\n'
        before = 'ota:\n  - platform: esphome\nwifi:\n  password: "housepassword1"\n'
        out = splice_esphome_text(
            before, disk, {"housepassword1"}, set(), allow_literal_credentials=True)
        assert out == before
        assert "Kx9fQ2mVb7" not in out

    def test_the_waiver_is_off_by_default(self):
        # The agent-facing path must be unchanged: no keyword, no waiver.
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(self.DISK, self.DISK, {"housepassword1"}, {"wifi_password"})

    def test_it_waives_only_literals_not_the_placeholder_rules(self):
        # A placeholder with no counterpart is a broken write in any mode.
        new = 'api:\n  encryption:\n    key: __PHOENIX_REDACTED__api.encryption.key__\n'
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(
                new, "api:\n  encryption:\n    key: !secret k\n", set(), {"k"},
                allow_literal_credentials=True)

    def test_it_waives_only_literals_not_the_secret_existence_check(self):
        new = 'api:\n  encryption:\n    key: !secret nope\n'
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(
                new, self.DISK, set(), {"wifi_password"}, allow_literal_credentials=True)


class TestMigrationCarveOut:
    DISK = 'api:\n  encryption:\n    key: "REALKEY123456="\n'

    def test_inline_to_defined_secret_ref_allowed(self):
        red = redact_esphome_text(self.DISK)[0]
        new = red.replace("__PHOENIX_REDACTED__api.encryption.key__", "!secret rf_api_key")
        out = splice_esphome_text(new, self.DISK, set(), {"rf_api_key"})
        assert "key: !secret rf_api_key" in out
        assert "REALKEY123456=" not in out

    def test_undefined_secret_ref_refused_naming_the_two_step_fix(self):
        red = redact_esphome_text(self.DISK)[0]
        new = red.replace("__PHOENIX_REDACTED__api.encryption.key__", "!secret rf_api_key")
        with pytest.raises(EsphomeSecretViolation) as exc:
            splice_esphome_text(new, self.DISK, set(), {"wifi_ssid", "wifi_password"})
        msg = str(exc.value)
        assert "rf_api_key" in msg
        assert "secrets.yaml" in msg
        # The human adds the key first; the agent then does the mechanical swap.
        assert "currently inline" in msg

    def test_secret_ref_not_validated_when_secrets_unreadable(self):
        red = redact_esphome_text(self.DISK)[0]
        new = red.replace("__PHOENIX_REDACTED__api.encryption.key__", "!secret anything")
        # secret_keys=None means secrets.yaml could not be read; degrade rather
        # than refusing every !secret write.
        out = splice_esphome_text(new, self.DISK, set(), None)
        assert "!secret anything" in out


class TestScrubHelpers:
    """The free-text scrub used on Device Builder output.

    Validation errors quote the offending YAML line back, so a file carrying an
    inline credential can print it into a message that has no structure to
    splice; a blind value replace is the only tool that works there.
    """

    FILE = '''esphome:
  name: dev
api:
  encryption:
    key: "INLINEKEY1234567="
wifi:
  password: !secret wifi_password
  ssid: MyNetworkName
'''

    def test_returns_values_not_spans(self):
        values = inline_secret_values(self.FILE)
        # The value itself, unquoted, is what has to be matched in free text.
        assert "INLINEKEY1234567=" in values
        assert '"INLINEKEY1234567="' not in values

    def test_secrets_yaml_values_are_included_when_inlined(self):
        values = inline_secret_values(self.FILE, {"MyNetworkName"})
        assert "MyNetworkName" in values

    def test_short_values_never_scrubbed(self):
        # A blind substring replace over arbitrary output would corrupt it.
        short = 'esphome:\n  name: dev\nsensor:\n  - platform: gpio\n    password: "abc"\n'
        assert inline_secret_values(short) == set()

    def test_secret_refs_are_not_values(self):
        assert not any("!secret" in v for v in inline_secret_values(self.FILE))

    def test_scrub_replaces_every_occurrence(self):
        out = scrub_secret_values("saw pw1234567 twice: pw1234567", {"pw1234567"})
        assert "pw1234567" not in out
        assert out.count("<redacted>") == 2

    def test_scrub_replaces_longest_value_first(self):
        # Replacing the short one first would leave a readable remainder of the
        # long one behind.
        out = scrub_secret_values("value=abcdefgh12345", {"abcdefgh", "abcdefgh12345"})
        assert "abcdefgh" not in out

    def test_scrub_leaves_unrelated_text_alone(self):
        out = scrub_secret_values("ERROR at line 12: bad pin", {"housepassword1"})
        assert out == "ERROR at line 12: bad pin"

    def test_unparseable_file_raises_so_callers_decide(self):
        with pytest.raises(YamlParseError):
            inline_secret_values("esphome:\n  name: [unclosed\n")


# --------------------------------------------------------------------------- #
# !phoenix_generate: Phoenix generates rather than accepts
# --------------------------------------------------------------------------- #

class TestPhoenixGenerate:
    """A model does not produce cryptographic randomness.

    So an agent authoring a new device asks for a credential with a tag and
    never chooses, sees, or learns the value: substitution happens as the file
    is written, and every later read masks the result.
    """

    NEW_DEVICE = (
        "esphome:\n  name: newdev\n"
        "api:\n  encryption:\n    key: !phoenix_generate\n"
        "ota:\n  - platform: esphome\n    password: !phoenix_generate\n"
        "wifi:\n  ssid: !secret wifi_ssid\n"
    )

    def test_the_api_key_is_a_real_esphome_key(self):
        """ESPHome accepts exactly 32 base64-encoded bytes and nothing else."""
        import base64

        out = splice_esphome_text(self.NEW_DEVICE, "", set(), {"wifi_ssid"})
        key = out.split("key: ")[1].split("\n")[0].strip().strip('"')
        assert len(base64.b64decode(key)) == 32
        assert "!phoenix_generate" not in out

    def test_two_writes_never_produce_the_same_value(self):
        """Real randomness, not a fixed or derived placeholder."""
        keys = {
            splice_esphome_text(self.NEW_DEVICE, "", set(), {"wifi_ssid"}).split("key: ")[1]
            for _ in range(5)
        }
        assert len(keys) == 5

    def test_the_agent_never_sees_what_was_generated(self):
        """The whole point: the value exists on disk and nowhere the caller looks."""
        out = splice_esphome_text(self.NEW_DEVICE, "", set(), {"wifi_ssid"})
        masked, paths = redact_esphome_text(out, set())

        assert "api.encryption.key" in paths
        assert "ota[0].password" in paths
        for line in masked.splitlines():
            if "key:" in line or "password:" in line:
                assert "__PHOENIX_REDACTED__" in line or "!secret" in line, line

    def test_a_generated_value_is_then_frozen_like_any_other(self):
        """It joins the existing write-freeze rather than being a special case."""
        on_disk = splice_esphome_text(self.NEW_DEVICE, "", set(), {"wifi_ssid"})
        attacker = on_disk.replace(
            on_disk.split("key: ")[1].split("\n")[0], '"ATTACKERCHOSENKEY="')
        with pytest.raises(EsphomeSecretViolation):
            splice_esphome_text(attacker, on_disk, set(), {"wifi_ssid"})

    def test_regenerating_an_existing_credential_is_refused(self):
        """Rotating a key a device is already running takes it off the network
        until Home Assistant is given the new one, and can need a cable.
        """
        disk = 'api:\n  encryption:\n    key: "ALREADYSETKEY123="\n'
        with pytest.raises(EsphomeSecretViolation) as err:
            splice_esphome_text('api:\n  encryption:\n    key: !phoenix_generate\n', disk)
        assert "already holds a credential" in str(err.value)

    @pytest.mark.parametrize("path_yaml", [
        "wifi:\n  password: !phoenix_generate\n",
        "wifi:\n  ssid: !phoenix_generate\n",
        "wifi:\n  networks:\n    - password: !phoenix_generate\n",
    ])
    def test_the_house_wifi_credentials_are_never_generated(self, path_yaml):
        """These belong to the house, not the device: randomising one would take
        every device sharing the secret off the network at once.
        """
        with pytest.raises(EsphomeSecretViolation) as err:
            splice_esphome_text(path_yaml, "")
        assert "house" in str(err.value)

    @pytest.mark.parametrize("path_yaml", [
        "esphome:\n  name: !phoenix_generate\n",
        "logger:\n  level: !phoenix_generate\n",
        "spi:\n  clk_pin: !phoenix_generate\n",
    ])
    def test_non_credential_fields_are_refused(self, path_yaml):
        """Not a general-purpose randomiser: it only fills credential values."""
        with pytest.raises(EsphomeSecretViolation) as err:
            splice_esphome_text(path_yaml, "")
        assert "not a credential field" in str(err.value)

    def test_the_generated_value_is_quoted(self):
        """Base64 can end in '=' and tokens can start with anything; quoting
        unconditionally means a value can never alter the document's shape.
        """
        out = splice_esphome_text(
            "api:\n  encryption:\n    key: !phoenix_generate\n", "")
        value = out.split("key: ")[1].strip()
        assert value.startswith('"') and value.endswith('"')

    def test_the_rest_of_the_file_is_untouched(self):
        """Span-surgical like every other write in this module."""
        out = splice_esphome_text(self.NEW_DEVICE, "", set(), {"wifi_ssid"})
        assert "esphome:\n  name: newdev\n" in out
        assert "!secret wifi_ssid" in out
        assert "- platform: esphome" in out
