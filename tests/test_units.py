import gzip
import tempfile
import unittest
from pathlib import Path

from mcmap import nbt
from mcmap.arnis import GenerationRequest, build_command
from mcmap.jobs import OutputParser
from mcmap.worlds import safe_folder_name, unique_path

PAYLOAD = {
    "bbox": {"south": 48.85, "west": 2.29, "north": 48.86, "east": 2.30},
    "spawn": {"lat": 48.855, "lng": 2.295},
    "name": "Paris",
}


class NbtTest(unittest.TestCase):
    def test_roundtrip_all_tag_types(self):
        root = {
            "b": (nbt.BYTE, -5),
            "s": (nbt.SHORT, 1234),
            "i": (nbt.INT, -70000),
            "l": (nbt.LONG, 2**40),
            "f": (nbt.FLOAT, 1.5),
            "d": (nbt.DOUBLE, -2.25),
            "ba": (nbt.BYTE_ARRAY, [1, -2, 3]),
            "str": (nbt.STRING, "héllo \x00 🌍"),
            "list": (nbt.LIST, (nbt.INT, [1, 2, 3])),
            "empty": (nbt.LIST, (nbt.END, [])),
            "nested": (nbt.COMPOUND, {"x": (nbt.STRING, "y")}),
            "ia": (nbt.INT_ARRAY, [1, -1]),
            "la": (nbt.LONG_ARRAY, [2**50, -1]),
        }
        data = nbt.dumps("", (nbt.COMPOUND, root))
        name, (tag_type, parsed) = nbt.loads(data)
        self.assertEqual((name, tag_type, parsed), ("", nbt.COMPOUND, root))
        self.assertEqual(nbt.dumps(name, (tag_type, parsed)), data)

    def test_modified_utf8_matches_java(self):
        # Java writes NUL as C0 80 and astral characters as two 3-byte surrogates.
        self.assertEqual(nbt._mutf8_encode("a\x00"), b"a\xc0\x80")
        self.assertEqual(nbt._mutf8_encode("🌍"), b"\xed\xa0\xbc\xed\xbc\x8d")
        self.assertEqual(nbt._mutf8_decode(b"\xed\xa0\xbc\xed\xbc\x8d"), "🌍")

    def test_set_level_name_keeps_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "level.dat"
            data = {"LevelName": (nbt.STRING, "Arnis World 1"), "GameType": (nbt.INT, 1)}
            path.write_bytes(gzip.compress(nbt.dumps("", (nbt.COMPOUND, {"Data": (nbt.COMPOUND, data)}))))
            nbt.set_level_name(path, "Zürich 🏔")
            self.assertEqual(nbt.get_level_name(path), "Zürich 🏔")
            _, (_, root) = nbt.read_gzip(path)
            self.assertEqual(root["Data"][1]["GameType"], (nbt.INT, 1))


class RequestTest(unittest.TestCase):
    def test_defaults_build_java_command(self):
        req = GenerationRequest.from_dict(PAYLOAD)
        cmd = build_command(Path("/bin/arnis"), req, Path("/out"))
        self.assertEqual(cmd[0], "/bin/arnis")
        self.assertIn("--bbox=48.850000,2.290000,48.860000,2.300000", cmd)
        self.assertIn("--output-dir=/out", cmd)
        self.assertIn("--mode=geo-terrain", cmd)
        self.assertIn("--world-type=void", cmd)
        self.assertIn("--map-preview", cmd)
        self.assertIn("--spawn-lat=48.855000", cmd)
        self.assertNotIn("--bedrock", cmd)

    def test_bedrock_options(self):
        req = GenerationRequest.from_dict({**PAYLOAD, "edition": "bedrock", "caves": True, "props_3d": False,
                                           "mode": "terrain-only", "world_time": 18000, "scale": 0.5})
        cmd = build_command(Path("/bin/arnis"), req, Path("/out"))
        for flag in ("--bedrock", "--caves", "--no-3d", "--mode=terrain-only", "--world-time=18000", "--scale=0.5"):
            self.assertIn(flag, cmd)
        self.assertFalse(any(c.startswith(("--world-type", "--signage")) for c in cmd))

    def test_python_stub_runs_with_interpreter(self):
        req = GenerationRequest.from_dict(PAYLOAD)
        cmd = build_command(Path("/x/fake_arnis.py"), req, Path("/out"))
        self.assertTrue(cmd[0].endswith(("python", "python3")) or "python" in Path(cmd[0]).name)
        self.assertEqual(cmd[1], "/x/fake_arnis.py")

    def test_round_trip_through_dict(self):
        req = GenerationRequest.from_dict(PAYLOAD)
        self.assertEqual(GenerationRequest.from_dict(req.to_dict()), req)

    def test_rejects_bad_input(self):
        cases = [
            ({**PAYLOAD, "bbox": None}, "Select an area"),
            ({**PAYLOAD, "bbox": {"south": 1, "west": 1, "north": 0, "east": 2}}, "south < north"),
            ({**PAYLOAD, "spawn": {"lat": 10, "lng": 10}}, "inside the selected area"),
            ({**PAYLOAD, "bbox": {"south": 40, "west": -100, "north": 45, "east": -90}}, "too large"),
            ({**PAYLOAD, "edition": "pocket"}, "edition"),
            ({**PAYLOAD, "scale": 9}, "Scale"),
            ({**PAYLOAD, "scale": "1"}, "number"),
            ({**PAYLOAD, "caves": "yes"}, "true or false"),
            ({**PAYLOAD, "world_time": 24000}, "Time of day"),
        ]
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    GenerationRequest.from_dict(payload)


class OutputParserTest(unittest.TestCase):
    def test_parses_phases_progress_and_paths(self):
        p = OutputParser()
        lines = []
        stream = (
            "\r\n        ▄████████ banner\r\n"
            "\x1b[1m[1/7]\x1b[0m Fetching data...\r\n"
            "\x1b[1m[3/7]\x1b[0m Fetching elevation...\r\n"
            "\x1b[1m[3/7]\x1b[0m Fetching land cover...\r\n"
            "\x1b[1m[5/7]\x1b[0m Generating area...\r\n"
            "\r\x1b[2K⠋ [00:00:01] [####------] 50/200 elements (3s) "
            "\r\x1b[2K⠋ [00:00:02] [#######---] 150/200 elements (1s) "
        ).encode()
        # Feed in awkward chunk sizes so escape codes and UTF-8 characters get split.
        for i in range(0, len(stream), 7):
            lines += p.feed(stream[i : i + 7])
        self.assertEqual(p.step, 5)
        self.assertEqual(p.step_label, "Generating area...")
        expected = sum((0.12, 0.03, 0.15, 0.05)) + 0.40 * 0.75
        self.assertAlmostEqual(p.progress, expected, places=6)
        self.assertIn("[3/7] Fetching land cover...", lines)
        self.assertFalse(any("elements" in line for line in lines), "progress bar frames must not be logged")
        self.assertTrue(any("▄████████" in line for line in lines), "multi-byte characters survive chunking")

        lines = p.feed(
            b"\n\x1b[1m[7/7]\x1b[0m Saving world...\r\n"
            b"Created new world at: \x1b[1m/tmp/out/Arnis World 1\x1b[0m\r\n"
            b"Map preview saved to: /tmp/out/Arnis World 1/arnis_world_map.png\r\n"
            b"\x1b[31mError:\x1b[0m something odd"
        )
        lines += p.flush()
        self.assertEqual(p.world_path, "/tmp/out/Arnis World 1")
        self.assertEqual(p.preview_path, "/tmp/out/Arnis World 1/arnis_world_map.png")
        self.assertEqual(p.errors, ["something odd"])
        self.assertIn("Error: something odd", lines)

    def test_progress_never_goes_backwards(self):
        p = OutputParser()
        p.feed(b"[5/7] Generating area...\n[####] 90/100 elements\r")
        high = p.progress
        p.feed(b"[####] 10/100 elements\r")
        self.assertGreaterEqual(p.progress, high)

    def test_bedrock_path(self):
        p = OutputParser()
        p.feed(b"\x1b[32mDone!\x1b[0m Bedrock world saved to: /d/Arnis X.mcworld\n")
        self.assertEqual(p.world_path, "/d/Arnis X.mcworld")


class WorldsTest(unittest.TestCase):
    def test_safe_folder_name(self):
        self.assertEqual(safe_folder_name('a/b:c*"d"'), "a_b_c__d_")
        self.assertEqual(safe_folder_name("  Paris. "), "Paris")
        self.assertEqual(safe_folder_name("CON"), "Minecraft World")
        self.assertEqual(safe_folder_name(""), "Minecraft World")

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertEqual(unique_path(d, "w").name, "w")
            (d / "w").mkdir()
            (d / "w (2)").mkdir()
            self.assertEqual(unique_path(d, "w").name, "w (3)")
            self.assertEqual(unique_path(d, "x", ".mcworld").name, "x.mcworld")


if __name__ == "__main__":
    unittest.main()
