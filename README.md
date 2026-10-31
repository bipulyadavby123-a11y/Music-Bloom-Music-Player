# Music Bloom 🎵

## A Music Player Application Based on Circular Doubly Linked List

Music Bloom is a Python-based desktop music application developed using CustomTkinter. The playlist is implemented using a **Circular Doubly Linked List (CDLL)**, allowing users to move forward and backward through songs continuously.

## 1. Data Structure Used: Circular Doubly Linked List

A Circular Doubly Linked List is a linked data structure in which each node contains:
- Song title
- Artist name
- File path
- A pointer to the previous node (`prev`)
- A pointer to the next node (`next`)

The last node points to the first node, and the first node points back to the last node. This creates a circular structure.

### Structure

```text
       ┌──────────────────────────────────────┐
       ↓                                      │
   Song A  ⇄  Song B  ⇄  Song C
       ↑                                      ↓
       └──────────────────────────────────────┘
```

Each song node is connected in both directions. Therefore, the playlist supports forward and backward navigation.

## 2. Operations Implemented

| Operation | Description |
|---|---|
| Add Song | Inserts a new song into the circular doubly linked list. |
| Remove Song | Removes a selected song and reconnects the neighbouring nodes. |
| Next Song | Moves the current pointer to the next song. |
| Previous Song | Moves the current pointer to the previous song. |
| Search Song | Searches songs by title or artist name. |
| Display Playlist | Traverses the circular list to display all songs. |

## 3. Time Complexity

| Operation | Time Complexity |
|---|---|
| Add Song | O(1) |
| Remove Song | O(1), when the node is available |
| Next Song | O(1) |
| Previous Song | O(1) |
| Search Song | O(n) |
| Display Playlist | O(n) |

## 4. Features

- Desktop graphical interface using CustomTkinter
- Local music playback
- Playlist management using Circular Doubly Linked List
- Next and Previous song navigation
- Online music browsing
- Audio equalizer with multiple presets
- Animated vinyl record
- Spotify shortcut that opens Spotify in a web browser

## 5. Technologies Used

- Python 3.12
- CustomTkinter
- Pillow
- NumPy
- SciPy
- SoundFile
- SoundDevice
- FFmpeg

## 6. Requirements

- macOS
- Python 3.12 recommended
- FFmpeg

Install FFmpeg using Homebrew:

```bash
brew install ffmpeg
```

## 7. Installation

Clone the repository:

```bash
git clone https://github.com/bipulyadavby123-a11y/Music-Bloom-Music-Player.git
cd Music-Bloom-Music-Player
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

## 8. Run the Application

```bash
python music_bloom_glass.py
```

## 9. Project Structure

```text
Music-Bloom-Music-Player/
├── assets/
├── music_bloom_glass.py
├── online_music.py
├── vinyl_record.png
├── requirements.txt
├── Music Bloom.spec
├── MusicBloom.icns
└── Start Music Player.command
```

## 10. Advantages of Circular Doubly Linked List

- Supports movement in both forward and backward directions.
- Provides continuous circular playlist navigation.
- Adding a song at the end is efficient.
- Removing a known node is efficient.
- No shifting of elements is required as in an array-based playlist.

## 11. Conclusion

Music Bloom demonstrates the practical use of a Circular Doubly Linked List in a music player application. It uses song nodes connected through previous and next pointers to provide continuous playlist navigation. This project shows how data structures can be applied to develop a real-world desktop application.

## Author

Bipul Yadav
