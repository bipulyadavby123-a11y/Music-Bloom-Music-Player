# Music Bloom 🎵

Music Bloom is a Python-based desktop music player built with CustomTkinter. It includes local music playback, online music browsing, an audio equalizer, and an animated vinyl record.

## Features

- Desktop graphical interface using CustomTkinter
- Local music playback
- Online music browsing
- Audio equalizer with multiple presets
- Animated vinyl record
- Spotify shortcut that opens Spotify in a web browser

## Technologies Used

- Python 3.12
- CustomTkinter
- Pillow
- NumPy
- SciPy
- SoundFile
- SoundDevice
- FFmpeg

## Requirements

- macOS
- Python 3.12 recommended
- FFmpeg

Install FFmpeg using Homebrew:

```bash
brew install ffmpeg
```

## Installation

Clone the repository:

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd MusicPlayerGUI
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the required Python packages:

```bash
python -m pip install -r requirements.txt
```

## Run the Application

```bash
python music_bloom_glass.py
```

## Project Structure

```text
MusicPlayerGUI/
├── assets/
├── music_bloom_glass.py
├── online_music.py
├── vinyl_record.png
├── requirements.txt
├── Music Bloom.spec
├── MusicBloom.icns
└── Start Music Player.command
```

## Notes

- The audio equalizer requires the audio-related Python packages listed in requirements.txt.
- FFmpeg is an external system dependency and must be installed separately.
- The PyInstaller spec file is included for macOS application packaging.

## Author

Bipul Yadav
