# MCMap

Select an area on a map and turn it into a Minecraft world, all on your own machine.

MCMap is a local alternative to [MapSmith](https://arnismc.com/mapsmith/). Both are built on
[Arnis](https://github.com/louis-e/arnis) (Apache-2.0), the open-source engine that turns
OpenStreetMap data and real elevation into Minecraft worlds. MCMap adds a map UI, a job queue
and world management on top of the Arnis command-line tool.

## Requirements

- Python 3.9 or newer. MCMap uses only the standard library, so there is nothing to `pip install`.
- An internet connection, for map tiles, OpenStreetMap data and elevation.
- Minecraft Java Edition 1.17+ (use the latest release; a few optional features need 1.21.4+)
  or Bedrock Edition to play the result.

## Run it

```sh
python -m mcmap
```

MCMap opens at <http://localhost:8765>. The first time you generate a world, it downloads the
Arnis engine (v3.3.0, about 63 MB) into `data/bin/`. You can also start that download from the
banner at the top of the side panel.

## Using it

1. **Choose an area.** Search for a place, then pick a preset size (Neighborhood 0.5 km up to
   Metro 8 km) or draw your own rectangle. Drag the corner handles to resize it and the center
   handle to move it.
2. **Spawn point (optional).** Click *Pick on map*, then click inside the area.
3. **World settings.** Choose the name, Java or Bedrock, what to generate (objects + terrain,
   objects only, or terrain only), game mode and time of day. *Advanced* has scale, terrain
   height, interiors, caves, 3D props, signs and more.
4. **Generate.** Progress shows live under *Your worlds*. When a world is done:
   - **Java:** it is moved into your Minecraft saves folder automatically, so it appears in the
     singleplayer list. You can turn that off and use *Add to Minecraft* later, or download a `.zip`.
   - **Bedrock:** download the `.mcworld` file and open it on a device that has Minecraft Bedrock.

Jobs run one at a time, and extra jobs wait in a queue. Job history survives restarts.

## Options

```text
python -m mcmap [--port 8765] [--host 127.0.0.1] [--no-browser]
                [--arnis PATH] [--saves-dir PATH] [--data-dir PATH]
```

| Option | What it does |
| --- | --- |
| `--arnis PATH` | Use your own Arnis executable (for example, one you built from source) instead of the managed download. |
| `--saves-dir PATH` | Minecraft Java saves folder. Detected automatically for the default launcher on macOS, Windows and Linux. |
| `--data-dir PATH` | Where MCMap keeps Arnis, unfinished or uninstalled worlds, and `jobs.json`. Defaults to `./data`. |
| `--host 0.0.0.0` | Make MCMap reachable from other devices on your network. By default it only listens on localhost. |

## How it works

```text
browser (Leaflet map)  ──JSON──▶  mcmap.server  ──▶  mcmap.jobs  ──▶  arnis --bbox … --output-dir …
                                   (stdlib HTTP)      (queue, PTY)        │
                                                                          ▼
                                   rename world, set LevelName ◀── data/worlds/<job>/Arnis World 1
                                   move to Minecraft saves / zip
```

- `mcmap/arnis.py` installs the pinned Arnis release, validates requests and builds the CLI command.
- `mcmap/jobs.py` runs Arnis in a pseudo-terminal so it draws its progress bars, parses that
  output into phase and percent, then renames the world and installs it.
- `mcmap/nbt.py` is a minimal NBT reader/writer. It sets the in-game world name in `level.dat`,
  because the Arnis CLI always names worlds "Arnis World N".
- `mcmap/server.py` serves the UI and API. It only answers requests addressed to localhost, and it
  refuses cross-site requests, so other websites can't start jobs or delete files.
- `mcmap/static/` holds the UI: plain HTML, CSS and JavaScript, with Leaflet vendored.

Place search and name suggestions use OpenStreetMap's Nominatim, and the base map uses
OpenStreetMap and Esri tiles. These are fine for personal use. Check their usage policies before
hosting MCMap for other people.

## Development

```sh
python -m unittest discover tests        # unit + end-to-end tests (uses the fake engine)
python -m mcmap --arnis tests/fake_arnis.py   # click through the UI without real generation
```

`tests/fake_arnis.py` imitates Arnis's output and writes a minimal world, so you can work on the
UI and job handling without downloading data. `FAKE_ARNIS_DELAY=2` slows it down, and
`FAKE_ARNIS_FAIL=1` makes it fail.

## Credits

World generation by [Arnis](https://github.com/louis-e/arnis) by louis-e and contributors,
Apache License 2.0. Map data © OpenStreetMap contributors.
