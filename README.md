# SkyShards local solver

Run the [greenhouse.skyshards.com](https://greenhouse.skyshards.com) greenhouse solver on your own computer.

The website normally sends your greenhouse layout to a shared server, which solves it
with a fixed time budget and a queue. This package is the same solver, running locally with
no queue, and you can give it as much time as you like.

## Setup

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>.
   On Windows tick **"Add python.exe to PATH"** in the installer.
2. Download and unzip this package anywhere.
3. Start it:
   - **Windows:** double-click `start.bat`
   - **macOS / Linux:** run `./start.sh` in a terminal (`chmod +x start.sh` first if needed)

   The first start creates a virtual environment and installs the dependencies. Later starts are instant.
4. Leave the window open. Open <https://greenhouse.skyshards.com>, and in the calculator turn on
   **Solve locally**. The panel shows "connected" when the website can see the server.

Chrome asks once for permission to let greenhouse.skyshards.com talk to a program on your computer.
Firefox works without a prompt. Safari may block the connection entirely.

## Contributing layouts back

After each solve the local server offers the finished layout to the public server. The
public server re-checks and re-scores it independently, so a long local solve can improve the
answer everyone gets. Only the request (grid cells, targets, priorities, effect weights)
and the layout are sent. To opt out, set `"contribute": false` in `config.json`.

## Options

Edit `config.json` and restart the server. Every setting is optional; a missing
or invalid one falls back to its default and prints a note.

| Setting | Default | Meaning |
| --- | --- | --- |
| `port` | `8765` | Port the server listens on (set the same port in the website panel) |
| `solver_threads` | `8` | CP-SAT search threads; lower it on a small laptop |
| `contribute` | `true` | `false` stops uploading better layouts |
| `extra_origins` | `[]` | Extra websites allowed to use this server |

How long a solve may run is not configured here. The website sends a time limit
with each solve and it is used as given, so change it in the **Local solver**
panel on the site.

The server only listens on `127.0.0.1`; nothing outside your machine can reach it.
It keeps no solution cache, so every solve starts fresh.

## Same solver, same answers?

The code, objective and scoring are identical to the public server. Solves are
time-budgeted and multi-threaded, so a different CPU can explore a different part of the
search space in the same seconds and return a different (equally valid) layout. Giving the
local solver more time is the point.

## License

Copyright (C) SkyShards contributors.

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU Affero General Public License as published by the Free Software Foundation,
version 3 of the License. See [LICENSE](LICENSE). If you run a modified version as a
network service, the AGPL requires you to offer its source to the users of that service.
