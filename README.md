# Jolt Scan

**Unofficial.** A local Windows app that finds hidden cuts in Minecraft videos with jolt cracking.

Jolt Scan reads the hunger bar shake ("jolt") in each frame, works out the game's GUI tick from it, and plots game uptime against video time. A straight line means no cuts. A jump means time is missing from the video.

## What it does

- Paste a YouTube link (downloaded with yt-dlp) or open a video file.
- Trim the part to scan. The app finds the HUD, crops it and scans every frame.
- Shows a diagonal uptime chart with zoom, gap highlights, a draggable playhead and a frame-exact video panel.
- Lists every jump. Mark each one as real, not a cut or unsure, and add a note.
- Exports the results as CSV or HTML.

Everything runs on your computer. The server listens on 127.0.0.1 only.

## Requirements

- Windows 10 or 11
- [Python 3](https://www.python.org/) (tested with 3.13 and 3.14)
- [Rust](https://rustup.rs/), to build the scanner
- [FFmpeg](https://ffmpeg.org/) (ffmpeg and ffprobe) and [yt-dlp](https://github.com/yt-dlp/yt-dlp)

```bash
winget install Python.Python.3.13 Rustlang.Rustup Gyan.FFmpeg yt-dlp.yt-dlp
```

## Start

Run `Jolt Scan.bat`. The first time, it downloads the Tick Studio files the scanner needs (pinned to one commit and checked by hash), builds the scanner and runs its self-test. Then the app opens in your browser.

To set up the scanner again by hand:

```bash
python app/setup_scanner.py
```

Your videos and scans stay in the `projects` folder, which is not part of the repo.

## Credits

- [Jolt Cracker](https://dartjs.com/joltcracker): the jolt cracking method and the site that the chart and cracker in this app are modeled on.
- [Tick Studio](https://gitlab.com/fanda8571/tick_studio): the Java Random port, the hunger icon matcher and the hunger textures used by the scanner. These files are downloaded at setup and are not stored here.
- [I Found a Bug in the Dream Investigation](https://youtu.be/B3AUMY5IVLo): explains the `nextInt` overflow case that the scanner handles.

## Disclaimer

Jolt Scan is an unofficial fan project. It is not affiliated with or endorsed by the Jolt Cracker site or its authors, Tick Studio, Mojang Studios or Microsoft. Minecraft is a trademark of Microsoft and Mojang. A jump in the chart is evidence to check by eye, not proof by itself.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). You can use, change and share the code for noncommercial purposes. You cannot sell it or use it commercially. The license does not cover the Tick Studio files or the Minecraft textures.
