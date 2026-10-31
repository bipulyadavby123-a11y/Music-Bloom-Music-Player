import time
import os
import json
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from online_music import OnlineMusicWindow

try:
    import numpy as np
    import soundfile as sf
    import sounddevice as sd
    from scipy.signal import tf2sos, sosfilt
    AUDIO_ENGINE_AVAILABLE = True
except ImportError:
    np = sf = sd = None
    tf2sos = sosfilt = None
    AUDIO_ENGINE_AVAILABLE = False

PYGAME_AVAILABLE = AUDIO_ENGINE_AVAILABLE
pygame = None




# -------------------- REAL-TIME AUDIO / EQUALIZER ENGINE --------------------

EQ_FREQUENCIES = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_PRESETS = {
    "Flat":       [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Rock":       [4, 3, 2, 0, -1, 1, 2, 3, 4, 4],
    "Pop":        [-1, 1, 3, 4, 2, 0, 1, 2, 3, 2],
    "Jazz":       [3, 2, 1, 2, -1, -1, 0, 1, 2, 3],
    "Classical":  [4, 3, 2, 1, -1, -1, 0, 2, 3, 4],
    "Vocal":      [-2, -1, 0, 2, 3, 3, 2, 1, 0, -1],
}

class AudioEngine:
    """Callback-based audio player with ten peaking EQ filters."""
    def __init__(self):
        self.lock = threading.RLock()
        self.data = None
        self.samplerate = 44100
        self.position = 0
        self.duration = 0.0
        self.loop = False
        self.paused = True
        self.finished = False
        self.eq_on = True
        self.gains = [0.0] * 10
        self.preamp_db = 0.0
        self.volume = 0.8
        self.stream = None
        self.filters = []
        self.zi = []
        self._build_filters()

    def _build_filters(self):
        if not AUDIO_ENGINE_AVAILABLE:
            return
        filters = []
        for freq, gain in zip(EQ_FREQUENCIES, self.gains):
            # RBJ peaking-EQ biquad, converted to second-order sections.
            f0 = min(float(freq), self.samplerate * 0.45)
            A = 10.0 ** (float(gain) / 40.0)
            w0 = 2.0 * np.pi * f0 / self.samplerate
            alpha = np.sin(w0) / (2.0 * 1.4)
            cosw = np.cos(w0)
            b0 = 1 + alpha * A
            b1 = -2 * cosw
            b2 = 1 - alpha * A
            a0 = 1 + alpha / A
            a1 = -2 * cosw
            a2 = 1 - alpha / A
            sos = tf2sos([b0/a0, b1/a0, b2/a0], [1.0, a1/a0, a2/a0])
            filters.append(sos)
        self.filters = filters
        channels = 2 if self.data is None else self.data.shape[1]
        self.zi = [
            np.zeros((f.shape[0], 2, channels), dtype=np.float64)
            for f in filters
        ]

    def set_eq(self, gains=None, enabled=None, preamp=None, volume=None):
        if not AUDIO_ENGINE_AVAILABLE:
            return
        with self.lock:
            if gains is not None:
                self.gains = [float(np.clip(x, -12, 12)) for x in gains]
                self._build_filters()
            if enabled is not None:
                self.eq_on = bool(enabled)
            if preamp is not None:
                self.preamp_db = float(np.clip(preamp, -12, 12))
            if volume is not None:
                self.volume = float(np.clip(volume, 0, 1))

    def load(self, filepath, loop=False, start=0.0, autoplay=True):
        if not AUDIO_ENGINE_AVAILABLE:
            raise RuntimeError(
                "Audio libraries missing. Install numpy, scipy, soundfile, and sounddevice."
            )

        self.stop()
        data, sr = sf.read(filepath, dtype="float32", always_2d=True)
        if data.size == 0:
            raise RuntimeError("This audio file contains no decodable audio.")

        # Use the current macOS default output device.
        device = sd.default.device[1]
        if device is None or device < 0:
            device = sd.query_devices(kind="output")

        info = sd.query_devices(device, "output")
        channels = min(2, int(info["max_output_channels"]))
        if channels < 1:
            raise RuntimeError(f"No output channels available for {info['name']}.")

        # Convert source audio to the number of channels supported by the device.
        if channels == 1 and data.shape[1] > 1:
            data = data.mean(axis=1, keepdims=True)
        elif channels == 2 and data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > channels:
            data = data[:, :channels]

        output_rate = int(info["default_samplerate"])
        if output_rate <= 0:
            output_rate = int(sr)

        if int(sr) != output_rate:
            from scipy.signal import resample_poly
            from math import gcd
            common = gcd(int(sr), output_rate)
            data = resample_poly(
                data, output_rate // common, int(sr) // common, axis=0
            ).astype("float32")
            sr = output_rate

        with self.lock:
            self.data = data
            self.samplerate = int(sr)
            self.output_channels = channels
            self.duration = len(data) / float(sr)
            self.position = int(max(0, min(start, self.duration)) * sr)
            self.loop = bool(loop)
            self.paused = not autoplay
            self.finished = False
            self._build_filters()
            self.zi = [
                np.zeros((f.shape[0], 2, data.shape[1]), dtype=np.float64)
                for f in self.filters
            ]

        self.stream = sd.OutputStream(
            device=device,
            samplerate=self.samplerate,
            channels=channels,
            dtype="float32",
            blocksize=1024,
            callback=self._callback
        )
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if self.data is None or self.paused or self.finished:
                outdata.fill(0)
                return
            start = self.position
            end = start + frames
            if start >= len(self.data):
                if self.loop and len(self.data):
                    start = 0
                    end = frames
                    self.position = 0
                    self.zi = [np.zeros((s.shape[0], 2, self.data.shape[1]), dtype=np.float64) for s in self.filters]
                else:
                    self.finished = True
                    self.paused = True
                    outdata.fill(0)
                    return
            chunk = self.data[start:min(end, len(self.data))].copy()
            valid = len(chunk)
            if valid < frames:
                if self.loop and len(self.data):
                    remain = frames - valid
                    chunk = np.concatenate((chunk, self.data[:remain]))
                    self.position = remain
                    self.zi = [np.zeros((s.shape[0], 2, self.data.shape[1]), dtype=np.float64) for s in self.filters]
                else:
                    self.position = len(self.data)
                    self.finished = True
                    self.paused = True
            else:
                self.position = end
            if self.eq_on:
                for idx, sos in enumerate(self.filters):
                    chunk, self.zi[idx] = sosfilt(sos, chunk, axis=0, zi=self.zi[idx])
            gain = (10.0 ** (self.preamp_db / 20.0)) * self.volume
            chunk *= gain
            outdata.fill(0)
            outdata[:min(frames, len(chunk))] = np.clip(chunk[:frames], -1.0, 1.0)

    def pause(self):
        with self.lock:
            self.paused = True

    def resume(self):
        with self.lock:
            if self.data is not None and not self.finished:
                self.paused = False

    def seek(self, seconds):
        with self.lock:
            if self.data is None:
                return
            self.position = int(np.clip(seconds, 0, self.duration) * self.samplerate)
            self.finished = self.position >= len(self.data)
            if self.finished:
                self.position = max(0, len(self.data) - 1)
                self.finished = False
            self.zi = [np.zeros((s.shape[0], 2, self.data.shape[1]), dtype=np.float64) for s in self.filters]

    def stop(self):
        stream, self.stream = self.stream, None
        with self.lock:
            self.paused = True
            self.finished = False
            self.data = None
            self.position = 0
            self.duration = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def get_position(self):
        with self.lock:
            return self.position / float(self.samplerate) if self.samplerate else 0.0

    def is_finished(self):
        with self.lock:
            return self.finished


# -------------------- THEME --------------------

BG = "#0B0B16"
PANEL = "#151526"
CARD = "#1D1D34"
BORDER = "#33334D"

PURPLE = "#A78BFA"
PINK = "#F472B6"
CYAN = "#67E8F9"
WHITE = "#F8FAFC"
MUTED = "#A1A1B5"
GREEN = "#6EE7B7"


# -------------------- CIRCULAR DOUBLY LINKED LIST --------------------

class SongNode:
    def __init__(self, title, artist, filepath):
        self.title = title
        self.artist = artist
        self.filepath = filepath
        self.prev = None
        self.next = None


class CircularDoublyLinkedList:
    def __init__(self):
        self.head = None
        self.current = None
        self.size = 0

    def add_song(self, title, artist, filepath):
        node = SongNode(title, artist, filepath)

        if self.head is None:
            node.next = node
            node.prev = node
            self.head = node
            self.current = node
        else:
            tail = self.head.prev
            node.prev = tail
            node.next = self.head
            tail.next = node
            self.head.prev = node

        self.size += 1
        return node

    def get_all(self):
        songs = []

        if self.head is None:
            return songs

        node = self.head

        while True:
            songs.append(node)
            node = node.next

            if node == self.head:
                break

        return songs

    def find(self, query):
        query = query.casefold().strip()

        if not query:
            return self.get_all()

        return [
            song for song in self.get_all()
            if query in song.title.casefold()
            or query in song.artist.casefold()
        ]

    def remove(self, node):
        if node is None or self.head is None:
            return False

        if node.next == node:
            self.head = None
            self.current = None
        else:
            node.prev.next = node.next
            node.next.prev = node.prev

            if self.head == node:
                self.head = node.next

            if self.current == node:
                self.current = node.next

        node.prev = None
        node.next = None
        self.size -= 1

        return True

    def next_song(self):
        if self.current:
            self.current = self.current.next

        return self.current

    def previous_song(self):
        if self.current:
            self.current = self.current.prev

        return self.current


# -------------------- MUSIC BLOOM APP --------------------

class MusicBloomApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("Music Bloom | Your Music Universe")

        # Restore Music Bloom window icon on macOS
        import os
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "assets", "music_icon.png")
        if os.path.exists(icon_path):
            try:
                self._music_bloom_icon = tk.PhotoImage(file=icon_path)
                self.iconphoto(True, self._music_bloom_icon)
            except Exception as exc:
                print("Music Bloom icon could not be loaded:", exc)
        self.geometry("1400x860")
        self.minsize(1100, 720)

        self.configure(fg_color=BG)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.playlist = CircularDoublyLinkedList()
        app_dir = os.path.dirname(os.path.abspath(__file__))
        self.playlist_file = os.path.join(app_dir, "music_playlist.json")
        self.database_file = os.path.join(app_dir, "music_bloom.db")
        self.load_playlist()
        self.is_playing = False
        self.loop_current = False
        self.row_widgets = []
        self.song_duration = 0.0
        self.seek_after_id = None
        self.user_seeking = False
        self.play_started_at = 0.0
        self.paused_at = 0.0
        self.position_before_pause = 0.0

        self.audio = AudioEngine()
        self.eq_settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_bloom_eq.json")
        self.eq_gains = list(EQ_PRESETS["Flat"])
        self.eq_preset = "Flat"
        self.eq_enabled = True
        self.bass_boost = False
        self.vocal_boost = False
        self.preamp_db = 0.0
        self.master_volume = 0.8
        self.load_eq_settings()
        self.audio.set_eq(self.eq_gains, self.eq_enabled, self.preamp_db, self.master_volume)

        self.build_ui()
        self.refresh_playlist()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.update_progress()
        self.after(500, self.check_song_end)

    def on_seek_preview(self, value):
        try:
            seconds = float(value)
            if hasattr(self, "current_time_label"):
                self.current_time_label.configure(
                    text=self.format_time(seconds)
                )
        except (ValueError, TypeError, AttributeError):
            pass

    def begin_seek(self, event=None):
        self.is_seeking = True

    def finish_seek(self, event=None):
        try:
            value = float(self.progress_slider.get())
            duration = float(getattr(self, "song_duration", 0) or 0)
            target = max(0.0, min(value, duration)) if duration > 0 else 0.0

            if hasattr(self, "audio") and hasattr(self.audio, "seek"):
                self.audio.seek(target)
            elif hasattr(self, "seek_audio"):
                self.seek_audio(target)

            self.position_before_pause = target
        except (ValueError, TypeError, AttributeError):
            pass
        finally:
            self.is_seeking = False

    def build_ui(self):
        # Main application shell: navigation sidebar + content area.
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(
            self, fg_color="#101020", corner_radius=0, width=205
        )
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)

        ctk.CTkLabel(
            self.sidebar, text="♫", font=("Helvetica", 42, "bold"),
            text_color=PINK
        ).pack(anchor="w", padx=22, pady=(25, 0))
        ctk.CTkLabel(
            self.sidebar, text="MUSIC BLOOM",
            font=("Helvetica", 18, "bold"), text_color=WHITE
        ).pack(anchor="w", padx=22, pady=(0, 4))
        ctk.CTkLabel(
            self.sidebar, text="YOUR MUSIC UNIVERSE",
            font=("Helvetica", 9), text_color=MUTED
        ).pack(anchor="w", padx=22, pady=(0, 28))

        self.sidebar_toggle = self.make_button(
            self.sidebar, "☰  Menu", self.toggle_sidebar,
            "#29243D", width=160
        )
        self.sidebar_toggle.pack(fill="x", padx=16, pady=(0, 18))

        self.make_button(
            self.sidebar, "⌂  Home",
            lambda: self.show_page("home"), "#25243A", width=165
        ).pack(fill="x", padx=16, pady=6)
        self.make_button(
            self.sidebar, "♫  Offline Playlist",
            lambda: self.show_page("offline"), "#25243A", width=165
        ).pack(fill="x", padx=16, pady=6)
        self.make_button(
            self.sidebar, "🎚  Equaliser",
            self.open_equalizer, "#25243A", width=165
        ).pack(fill="x", padx=16, pady=6)
        self.make_button(
            self.sidebar, "🟢  Spotify",
            lambda: self.show_page("spotify"), "#25243A", width=165
        ).pack(fill="x", padx=16, pady=6)

        ctk.CTkLabel(
            self.sidebar, text="PLAY YOUR WAY",
            font=("Helvetica", 10, "bold"), text_color=MUTED
        ).pack(side="bottom", pady=22)

        self.content = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(1, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.content, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 14))
        header.grid_columnconfigure(1, weight=1)
        self.menu_button = self.make_button(
            header, "☰", self.toggle_sidebar, "#29243D", width=42
        )
        self.menu_button.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.page_title = ctk.CTkLabel(
            header, text="Your Music Space",
            font=("Helvetica", 25, "bold"), text_color=WHITE
        )
        self.page_title.grid(row=0, column=1, sticky="w")
        self.count_label = ctk.CTkLabel(
            header, text="0 TRACKS", font=("Helvetica", 11, "bold"),
            text_color=CYAN, fg_color=CARD, corner_radius=12,
            padx=16, pady=10
        )
        self.count_label.grid(row=0, column=2, sticky="e")

        self.offline_page = ctk.CTkFrame(self.content, fg_color="transparent")
        self.offline_page.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 22))
        self.offline_page.grid_columnconfigure(0, weight=4)
        self.offline_page.grid_columnconfigure(1, weight=6)
        self.offline_page.grid_rowconfigure(0, weight=1)

        # Left: Now Playing / vinyl / playback controls.
        left = ctk.CTkFrame(
            self.offline_page, fg_color=PANEL, corner_radius=22,
            border_width=1, border_color=BORDER
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        ctk.CTkLabel(
            left, text="NOW PLAYING", font=("Helvetica", 12, "bold"),
            text_color=CYAN
        ).pack(anchor="w", padx=20, pady=(20, 12))

        self.art = ctk.CTkFrame(
            left, fg_color="#292044", corner_radius=22,
            border_width=1, border_color="#59427C", height=220
        )
        self.art.pack(fill="x", padx=20, pady=(0, 16))
        self.art.pack_propagate(False)
        self.vinyl_angle = 0
        self.vinyl_canvas = tk.Canvas(
            self.art, width=220, height=220, background="#292044",
            highlightthickness=0, bd=0
        )
        self.vinyl_canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self.draw_vinyl()
        self.vinyl_status = ctk.CTkLabel(
            self.art, text="MUSIC • MOOD • MOMENT",
            font=("Helvetica", 10, "bold"), text_color=WHITE
        )
        self.vinyl_status.place(relx=0.5, rely=0.92, anchor="center")
        self.after(80, self.animate_vinyl)

        self.now_title = ctk.CTkLabel(
            left, text="Your next favorite song",
            font=("Helvetica", 18, "bold"), text_color=WHITE,
            wraplength=330
        )
        self.now_title.pack(padx=14, pady=(0, 5))
        self.now_artist = ctk.CTkLabel(
            left, text="Add songs to get started", font=("Helvetica", 12),
            text_color=MUTED, wraplength=330
        )
        self.now_artist.pack(padx=14, pady=(0, 12))
        self.time_label = ctk.CTkLabel(
            left, text="00:00 / 00:00", font=("Helvetica", 11),
            text_color=MUTED
        )
        self.time_label.pack(fill="x", padx=22, pady=(0, 2))
        self.progress_slider = ctk.CTkSlider(
            left, from_=0, to=100, number_of_steps=1000,
            command=self.on_seek_preview, progress_color=PURPLE,
            button_color=PINK, button_hover_color=CYAN, fg_color=CARD
        )
        self.progress_slider.set(0)
        self.progress_slider.pack(fill="x", padx=22, pady=(0, 8))
        self.progress_slider.bind("<ButtonPress-1>", self.begin_seek)
        self.progress_slider.bind("<ButtonRelease-1>", self.finish_seek)

        controls = ctk.CTkFrame(left, fg_color="transparent")
        controls.pack(pady=7)
        self.make_button(controls, "⏮", self.previous_song, CARD, width=45).pack(side="left", padx=4)
        self.play_button = self.make_button(
            controls, "▶", self.toggle_play, PURPLE, width=58,
            text_color="#151526"
        )
        self.play_button.pack(side="left", padx=7)
        self.make_button(controls, "⏭", self.next_song, CARD, width=45).pack(side="left", padx=4)
        self.loop_button = self.make_button(
            left, "🔁 LOOP OFF", self.toggle_loop, CARD, width=120
        )
        self.loop_button.pack(pady=(3, 6))
        self.status_label = ctk.CTkLabel(
            left, text="Ready to play", font=("Helvetica", 11),
            text_color=GREEN, wraplength=330
        )
        self.status_label.pack(pady=(5, 12))

        # Right panel has tabs, keeping Online Music in the main window.
        right = ctk.CTkFrame(
            self.offline_page, fg_color=PANEL, corner_radius=22,
            border_width=1, border_color=BORDER
        )
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        tabbar = ctk.CTkFrame(right, fg_color="transparent")
        tabbar.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        ctk.CTkLabel(
            tabbar, text="Explore Music", font=("Helvetica", 20, "bold"),
            text_color=WHITE
        ).pack(side="left")
        self.make_button(
            tabbar, "🔎 Search Music", self.open_online_music,
            PURPLE, width=135, text_color="#151526"
        ).pack(side="right")

        self.library_panel = ctk.CTkFrame(right, fg_color="transparent")
        self.library_panel.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))
        self.library_panel.grid_rowconfigure(2, weight=1)
        self.library_panel.grid_columnconfigure(0, weight=1)

        toolbar = ctk.CTkFrame(self.library_panel, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 12))
        self.make_button(
            toolbar, "+ Add Songs", self.add_songs, PURPLE,
            width=120, text_color="#151526"
        ).pack(side="right")
        self.search_var = tk.StringVar()
        self.search_entry = ctk.CTkEntry(
            self.library_panel, textvariable=self.search_var,
            placeholder_text="Search your offline music...", height=40,
            corner_radius=12, border_color=BORDER, fg_color=CARD,
            text_color=WHITE
        )
        self.search_entry.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 10))
        self.search_entry.bind("<KeyRelease>", lambda event: self.refresh_playlist())
        self.rows_frame = ctk.CTkScrollableFrame(
            self.library_panel, fg_color="transparent", corner_radius=12
        )
        self.rows_frame.grid(row=2, column=0, sticky="nsew", padx=2, pady=(0, 8))
        ctk.CTkLabel(
            self.library_panel,
            text="Double-click a track to play it • Circular playlist navigation",
            font=("Helvetica", 10), text_color=MUTED
        ).grid(row=3, column=0, sticky="w", padx=6, pady=(0, 7))
        self.make_button(
            self.library_panel, "Remove Current Song", self.delete_selected,
            "#74334F", width=190
        ).grid(row=4, column=0, sticky="e", padx=6, pady=(0, 4))

        # Online Music opens in its own large window only when requested.
        self.online_music_window = None
        self.online_music_toplevel = None
        # Offline playlist stays in the sidebar section; Home keeps the player visible.
        self.current_music_tab = "library"
        self.show_music_tab("library")

        # Dedicated Spotify section; no account login or playlist API integration.
        self.spotify_page = ctk.CTkFrame(self.content, fg_color=PANEL, corner_radius=22)
        self.spotify_page.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 22))
        ctk.CTkLabel(
            self.spotify_page, text="SPOTIFY",
            font=("Helvetica", 28, "bold"), text_color=GREEN
        ).pack(anchor="w", padx=30, pady=(30, 8))
        ctk.CTkLabel(
            self.spotify_page,
            text="Your Spotify space",
            font=("Helvetica", 20, "bold"), text_color=WHITE
        ).pack(anchor="w", padx=30, pady=(0, 8))
        ctk.CTkLabel(
            self.spotify_page,
            text="Open Spotify to browse your music. Music Bloom playback and Spotify playback remain separate.",
            font=("Helvetica", 13), text_color=MUTED,
            wraplength=650, justify="left"
        ).pack(anchor="w", padx=30, pady=(0, 20))
        self.make_button(
            self.spotify_page, "Open Spotify ↗", self.open_spotify,
            GREEN, width=180, text_color="#102018"
        ).pack(anchor="w", padx=30, pady=(0, 12))
        ctk.CTkLabel(
            self.spotify_page,
            text="Spotify account login, playlists and playback integration are not connected in this version.",
            font=("Helvetica", 11), text_color=MUTED,
            wraplength=650, justify="left"
        ).pack(anchor="w", padx=30, pady=(8, 20))
        self.spotify_page.grid_remove()

    def toggle_sidebar(self):
        if getattr(self, "sidebar_collapsed", False):
            self.sidebar.grid()
            self.sidebar_collapsed = False
        else:
            self.sidebar.grid_remove()
            self.sidebar_collapsed = True

    def show_page(self, page):
        if page == "spotify":
            self.offline_page.grid_remove()
            self.spotify_page.grid()
            self.page_title.configure(text="Spotify")
            self.count_label.grid_remove()
        else:
            self.spotify_page.grid_remove()
            self.offline_page.grid()
            self.count_label.grid()
            if page == "offline":
                self.page_title.configure(text="Offline Playlist")
                self.show_music_tab("library")
            else:
                self.page_title.configure(text="Home")
                self.show_music_tab("library")

    def show_music_tab(self, tab):
        # The main content area never embeds the online search results.
        self.current_music_tab = "library"
        self.library_panel.grid()

    def open_spotify(self):
        import webbrowser
        try:
            webbrowser.open("https://open.spotify.com/")
            self.status_label.configure(text="Spotify opened in your browser.")
        except Exception as error:
            messagebox.showerror("Spotify", str(error))

    def draw_vinyl(self):
        """Load the local vinyl image."""
        from PIL import Image, ImageTk
        from pathlib import Path

        path = Path(__file__).resolve().with_name("vinyl_record.png")
        if not path.exists():
            print("Vinyl image missing:", path)
            return

        with Image.open(path) as source:
            self.vinyl_source = source.convert("RGBA").resize(
                (210, 210), Image.Resampling.LANCZOS
            )

        self.render_vinyl()

    def render_vinyl(self):
        """Render a glossy vinyl record with metallic and neon reflections."""
        import math
        from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter

        if not hasattr(self, "vinyl_source") or self.vinyl_source is None:
            return

        # Render at higher resolution, then scale down for smooth details.
        S = 3
        size = 210 * S
        disc = self.vinyl_source.resize(
            (size, size), Image.Resampling.LANCZOS
        ).convert("RGBA")

        cx = cy = size // 2
        outer = 103 * S

        # Add subtle, finely spaced record grooves.
        grooves = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grooves)

        for r in range(42 * S, 101 * S, 2 * S):
            alpha = 16 if (r // (2 * S)) % 2 else 28
            gd.ellipse(
                (cx-r, cy-r, cx+r, cy+r),
                outline=(190, 205, 255, alpha),
                width=max(1, S)
            )

        # Soft, glossy colored reflections over the black grooves.
        glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        gl = ImageDraw.Draw(glow)

        gl.arc(
            (cx-outer, cy-outer, cx+outer, cy+outer),
            start=205, end=300,
            fill=(30, 160, 255, 175), width=4*S
        )
        gl.arc(
            (cx-outer, cy-outer, cx+outer, cy+outer),
            start=25, end=115,
            fill=(255, 55, 165, 170), width=4*S
        )
        gl.arc(
            (cx-91*S, cy-91*S, cx+91*S, cy+91*S),
            start=215, end=285,
            fill=(90, 190, 255, 80), width=2*S
        )
        gl.arc(
            (cx-83*S, cy-83*S, cx+83*S, cy+83*S),
            start=35, end=105,
            fill=(255, 140, 195, 75), width=2*S
        )

        glow = glow.filter(ImageFilter.GaussianBlur(3*S))

        # Restrict the reflections to the circular disc.
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse(
            (7*S, 7*S, size-7*S, size-7*S),
            fill=255
        )
        glow.putalpha(
            Image.fromarray_alpha(mask)
            if hasattr(Image, "fromarray_alpha") else glow.getchannel("A")
        )

        # Composite the groove detail and restrained colored shine.
        disc.alpha_composite(grooves)
        disc.alpha_composite(glow)

        # A slim silver rim makes the edge look like polished vinyl.
        rim = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        rd = ImageDraw.Draw(rim)
        rd.ellipse(
            (6*S, 6*S, size-6*S, size-6*S),
            outline=(210, 205, 255, 130), width=2*S
        )
        rd.arc(
            (8*S, 8*S, size-8*S, size-8*S),
            start=205, end=300,
            fill=(90, 195, 255, 230), width=2*S
        )
        rd.arc(
            (8*S, 8*S, size-8*S, size-8*S),
            start=25, end=115,
            fill=(255, 105, 195, 230), width=2*S
        )
        disc.alpha_composite(rim)

        # Keep the red label from the original vinyl image.
        title = " ".join(
            str(getattr(self, "vinyl_title_text", "MUSIC BLOOM")).split()
        ).strip().upper() or "MUSIC BLOOM"

        if len(title) > 18:
            title = title[:17].rstrip() + "…"

        try:
            font = ImageFont.truetype(
                "/System/Library/Fonts/Helvetica.ttc", 11*S
            )
        except OSError:
            font = ImageFont.load_default()

        chars = list(title)
        count = len(chars)
        radius = int(size * 0.34)

        if count == 1:
            angles = [-90]
        else:
            span = min(145, max(85, count * 8))
            angles = [
                -90 - span / 2 + i * span / (count - 1)
                for i in range(count)
            ]

        for char, angle in zip(chars, angles):
            bbox = font.getbbox(char)
            w = max(12*S, bbox[2] - bbox[0] + 8*S)
            h = max(16*S, bbox[3] - bbox[1] + 8*S)

            glyph = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(glyph)
            d.text(
                (4*S - bbox[0], 4*S - bbox[1]),
                char,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=S,
                stroke_fill=(35, 8, 18, 255)
            )
            glyph = glyph.rotate(
                -angle - 90,
                resample=Image.Resampling.BICUBIC,
                expand=True
            )

            rad = math.radians(angle)
            x = int(cx + radius * math.cos(rad) - glyph.width / 2)
            y = int(cy + radius * math.sin(rad) - glyph.height / 2)

            if x >= 0 and y >= 0 and x+glyph.width <= size and y+glyph.height <= size:
                disc.alpha_composite(glyph, (x, y))

        # Downsample before rotating for smooth edges.
        disc = disc.resize((210, 210), Image.Resampling.LANCZOS)
        rotated = disc.rotate(
            -self.vinyl_angle,
            resample=Image.Resampling.BICUBIC
        )

        self.vinyl_photo = ImageTk.PhotoImage(rotated)
        self.vinyl_canvas.delete("all")
        self.vinyl_canvas.create_image(
            110, 110, image=self.vinyl_photo, anchor="center"
        )

    def set_vinyl_title(self, title):
        self.vinyl_title_text = str(title or "MUSIC BLOOM")
        self.render_vinyl()

    def animate_vinyl(self):
        """Rotate only while audio is playing."""
        try:
            if not self.winfo_exists():
                return
            if getattr(self, "is_playing", False):
                self.vinyl_angle = (self.vinyl_angle + 5) % 360
                self.render_vinyl()
            self.after(80, self.animate_vinyl)
        except tk.TclError:
            pass

    def format_time(self, seconds):
        seconds = max(0, int(float(seconds or 0)))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def play_song(self, node):
        self.is_online_playback = False
        if node is None:
            return
        if not os.path.isfile(node.filepath):
            messagebox.showwarning("File not found", node.filepath)
            return
        try:
            self.audio.load(
                node.filepath,
                loop=self.loop_current,
                autoplay=True
            )
            self.playlist.current = node
            self.song_duration = self.audio.duration
            self.position_before_pause = 0.0
            self.is_playing = True
            self.play_button.configure(text="⏸")
            self.now_title.configure(text=node.title)
            self.set_vinyl_title(node.title)
            self.now_artist.configure(text=node.artist)
            self.status_label.configure(text="Now playing")
            self.progress_slider.configure(from_=0, to=max(1, self.song_duration))
            self.progress_slider.set(0)
            self.save_playlist()
            self.refresh_playlist()

            # Save this successful local playback in SQLite history.
            try:
                with sqlite3.connect(self.database_file) as con:
                    con.execute("PRAGMA foreign_keys = ON")
                    con.execute("""
                        CREATE TABLE IF NOT EXISTS recently_played (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            song_id INTEGER NOT NULL,
                            played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (song_id) REFERENCES songs(id)
                                ON DELETE CASCADE
                        )
                    """)
                    row = con.execute(
                        "SELECT id FROM songs WHERE filepath = ?",
                        (node.filepath,)
                    ).fetchone()

                    if row:
                        con.execute(
                            "INSERT INTO recently_played (song_id) VALUES (?)",
                            (row[0],)
                        )
            except Exception as db_error:
                print("Recently Played database error:", db_error)

        except Exception as error:
            self.is_playing = False
            self.status_label.configure(text="Playback failed")
            messagebox.showerror(
                "Playback error",
                f"Could not play this file:\n{error}\n\n"
                "Check your audio libraries and output-device settings."
            )

    def toggle_play(self):
        node = self.playlist.current
        online = getattr(self, "is_online_playback", False)

        if self.is_playing:
            self.audio.pause()
            self.position_before_pause = self.audio.get_position()
            self.is_playing = False
            self.play_button.configure(text="▶")
            self.status_label.configure(text="Online audio paused" if online else "Paused")

        elif self.audio.data is not None and (online or node is not None):
            self.audio.resume()
            self.is_playing = True
            self.play_button.configure(text="⏸")
            self.status_label.configure(text="Playing online audio" if online else "Now playing")

        elif node is not None:
            self.play_song(node)

        else:
            songs = self.playlist.get_all()
            if songs:
                self.play_song(songs[0])
            else:
                self.status_label.configure(text="Add songs to get started")

    def next_song(self):
        node = self.playlist.next_song()
        if node:
            self.play_song(node)

    def previous_song(self):
        node = self.playlist.previous_song()
        if node:
            self.play_song(node)

    def toggle_loop(self):
        self.loop_current = not self.loop_current
        self.loop_button.configure(
            text="🔁 LOOP ON" if self.loop_current else "🔁 LOOP OFF"
        )
        if self.audio.data is not None:
            self.audio.loop = self.loop_current
        self.status_label.configure(
            text="Current track loops" if self.loop_current else "Loop disabled"
        )

    def add_songs(self):
        paths = filedialog.askopenfilenames(
            title="Add music",
            filetypes=[
                ("Audio files", "*.mp3 *.wav *.flac *.ogg *.aiff *.aif"),
                ("All files", "*.*")
            ]
        )
        added = 0
        for filepath in paths:
            if not os.path.isfile(filepath):
                continue
            if any(
                os.path.abspath(n.filepath) == os.path.abspath(filepath)
                for n in self.playlist.get_all()
            ):
                continue
            title = os.path.splitext(os.path.basename(filepath))[0]
            self.playlist.add_song(title, "Unknown Artist", filepath)
            added += 1
        if added:
            self.save_playlist()
            self.refresh_playlist()
            self.status_label.configure(text=f"Added {added} track(s)")
        elif paths:
            self.status_label.configure(text="Selected tracks were already in the library")

    def add_online_track(self, item, filepath):
        if not os.path.isfile(filepath):
            raise FileNotFoundError(filepath)
        title = item.get("title") or os.path.splitext(os.path.basename(filepath))[0]
        artist = item.get("creator") or "Unknown Artist"
        existing = next(
            (
                node for node in self.playlist.get_all()
                if os.path.abspath(node.filepath) == os.path.abspath(filepath)
            ),
            None
        )
        if existing is None:
            self.playlist.add_song(title, artist, filepath)
        self.save_playlist()
        self.refresh_playlist()
        self.status_label.configure(text=f"Added: {title}")

    def refresh_playlist(self):
        if not hasattr(self, "rows_frame"):
            return
        for widget in self.rows_frame.winfo_children():
            widget.destroy()
        query = self.search_var.get().strip() if hasattr(self, "search_var") else ""
        songs = self.playlist.find(query)
        self.row_widgets = []
        for index, node in enumerate(songs):
            row = ctk.CTkFrame(
                self.rows_frame,
                fg_color=CARD,
                corner_radius=10
            )
            row.pack(fill="x", padx=5, pady=4)
            row.grid_columnconfigure(0, weight=1)
            active = node == self.playlist.current
            label = ctk.CTkLabel(
                row,
                text=f"{'♫  ' if active else ''}{node.title}\n{node.artist}",
                anchor="w",
                justify="left",
                text_color=CYAN if active else WHITE
            )
            label.grid(row=0, column=0, sticky="ew", padx=12, pady=9)
            duration_text = "▶" if active else "Play"
            button = ctk.CTkButton(
                row,
                text=duration_text,
                width=60,
                command=lambda selected=node: self.play_song(selected)
            )
            button.grid(row=0, column=1, padx=8, pady=7)

            # Read the saved favourite state from SQLite.
            is_favorite = False
            try:
                with sqlite3.connect(self.database_file) as con:
                    fav_row = con.execute(
                        "SELECT is_favorite FROM songs WHERE filepath = ?",
                        (node.filepath,)
                    ).fetchone()
                    is_favorite = bool(fav_row and fav_row[0])
            except sqlite3.Error as error:
                print("Could not read favourite status:", error)

            favorite_button = ctk.CTkButton(
                row,
                text="♥" if is_favorite else "♡",
                width=42,
                fg_color="#74334F" if is_favorite else CARD,
                hover_color="#9D466A",
                command=lambda selected=node: self.toggle_favorite(selected)
            )
            favorite_button.grid(row=0, column=2, padx=(0, 8), pady=7)

            for widget in (row, label):
                widget.bind(
                    "<Double-Button-1>",
                    lambda event, selected=node: self.play_song(selected)
                )
            self.row_widgets.append(row)
        if hasattr(self, "count_label"):
            total = self.playlist.size
            self.count_label.configure(
                text=f"{total} TRACK{'S' if total != 1 else ''}"
            )

    def toggle_favorite(self, node):
        """Toggle and persist a song's favourite status in SQLite."""
        if node is None:
            return

        try:
            with sqlite3.connect(self.database_file) as con:
                con.execute("PRAGMA foreign_keys = ON")

                row = con.execute(
                    "SELECT id, is_favorite FROM songs WHERE filepath = ?",
                    (node.filepath,)
                ).fetchone()

                if row is None:
                    con.execute("""
                        INSERT INTO songs (title, artist, filepath, is_favorite)
                        VALUES (?, ?, ?, 1)
                    """, (node.title, node.artist, node.filepath))
                    new_state = True
                else:
                    new_state = not bool(row[1])
                    con.execute(
                        "UPDATE songs SET is_favorite = ? WHERE id = ?",
                        (int(new_state), row[0])
                    )

            self.refresh_playlist()
            self.status_label.configure(
                text=("Added to Favorites ♥" if new_state
                      else "Removed from Favorites")
            )

        except sqlite3.Error as error:
            messagebox.showerror(
                "Favorites error",
                f"Could not update favourite status:\n{error}"
            )


    def delete_selected(self):
        node = self.playlist.current
        if node is None:
            self.status_label.configure(text="No track to remove")
            return
        if not messagebox.askyesno(
            "Remove track",
            f'Remove "{node.title}" from your library?'
        ):
            return
        was_playing = self.is_playing
        if was_playing:
            self.audio.stop()
            self.is_playing = False
        self.playlist.remove(node)
        self.song_duration = 0
        self.position_before_pause = 0
        self.progress_slider.configure(to=100)
        self.progress_slider.set(0)
        self.save_playlist()
        self.refresh_playlist()
        if self.playlist.current is not None:
            self.play_song(self.playlist.current)
        else:
            self.now_title.configure(text="Your next favorite song")
            self.now_artist.configure(text="Add songs to get started")
            self.play_button.configure(text="▶")
            self.status_label.configure(text="Track removed")

    def update_progress(self):
        try:
            if self.audio.data is not None:
                position = self.audio.get_position()
                duration = self.audio.duration
                self.song_duration = duration
                if not self.user_seeking:
                    self.progress_slider.configure(to=max(1, duration))
                    self.progress_slider.set(min(position, duration))
                self.time_label.configure(
                    text=f"{self.format_time(position)} / {self.format_time(duration)}"
                )
        except (tk.TclError, AttributeError, ValueError):
            return
        self.after(250, self.update_progress)

    def check_song_end(self):
        try:
            if self.audio.is_finished():
                self.is_playing = False
                self.play_button.configure(text="▶")
                if getattr(self, "is_online_playback", False):
                    self.is_online_playback = False
                    self.status_label.configure(text="Online audio finished")
                elif self.loop_current:
                    self.play_song(self.playlist.current)
                elif self.playlist.current is not None:
                    self.next_song()
            self.after(500, self.check_song_end)
        except tk.TclError:
            pass

    def close_app(self):
        try:
            self.save_playlist()
            self.save_eq_settings()
            self.audio.stop()
            win = getattr(self, "online_music_toplevel", None)
            if win is not None and win.winfo_exists():
                win.destroy()
            if hasattr(self, "eq_window") and self.eq_window.winfo_exists():
                self.eq_window.destroy()
        except Exception:
            pass
        self.destroy()

    def load_eq_settings(self):
        try:
            with open(self.eq_settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            gains = data.get("gains", EQ_PRESETS["Flat"])
            if len(gains) == 10:
                self.eq_gains = [max(-12, min(12, float(x))) for x in gains]
            self.eq_preset = data.get("preset", "Custom")
            self.eq_enabled = bool(data.get("enabled", True))
            self.bass_boost = bool(data.get("bass_boost", False))
            self.vocal_boost = bool(data.get("vocal_boost", False))
            self.preamp_db = max(-12, min(12, float(data.get("preamp", 0))))
            self.master_volume = max(0, min(1, float(data.get("volume", 0.8))))
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def save_eq_settings(self):
        try:
            with open(self.eq_settings_path, "w", encoding="utf-8") as f:
                json.dump({
                    "gains": self.eq_gains, "preset": self.eq_preset,
                    "enabled": self.eq_enabled, "bass_boost": self.bass_boost,
                    "vocal_boost": self.vocal_boost, "preamp": self.preamp_db,
                    "volume": self.master_volume
                }, f, indent=2)
        except OSError as exc:
            print("Could not save equalizer settings:", exc)

    def open_equalizer(self):
        if not AUDIO_ENGINE_AVAILABLE:
            messagebox.showerror(
                "Equalizer unavailable",
                "Install the audio dependencies in this Python environment:\n\n"
                "python3 -m pip install numpy scipy soundfile sounddevice"
            )
            return
        if getattr(self, "eq_window", None) is not None:
            try:
                if self.eq_window.winfo_exists():
                    self.eq_window.lift()
                    return
            except Exception:
                pass

        win = ctk.CTkToplevel(self)
        self.eq_window = win
        win.title("Music Bloom | Studio Equalizer")
        win.geometry("850x520")
        win.minsize(760, 490)
        win.configure(fg_color=BG)
        win.transient(self)

        header = ctk.CTkFrame(win, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 8))
        ctk.CTkLabel(header, text="STUDIO EQUALIZER", font=("Helvetica", 22, "bold"),
                     text_color=WHITE).pack(side="left")
        self.eq_status = ctk.CTkLabel(header, text="REAL-TIME AUDIO", font=("Helvetica", 10, "bold"),
                                      text_color=CYAN)
        self.eq_status.pack(side="right")

        controls = ctk.CTkFrame(win, fg_color=PANEL, corner_radius=18, border_width=1, border_color=BORDER)
        controls.pack(fill="x", padx=24, pady=8)

        top = ctk.CTkFrame(controls, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(top, text="Preset", text_color=MUTED).pack(side="left", padx=(0, 8))
        self.preset_var = tk.StringVar(value=self.eq_preset if self.eq_preset in EQ_PRESETS else "Flat")
        self.preset_menu = ctk.CTkOptionMenu(top, variable=self.preset_var, values=list(EQ_PRESETS),
                                             command=self.apply_eq_preset, fg_color="#393052",
                                             button_color=PURPLE, button_hover_color=PINK)
        self.preset_menu.pack(side="left")
        self.eq_on_var = tk.BooleanVar(value=self.eq_enabled)
        ctk.CTkSwitch(top, text="EQ ON", variable=self.eq_on_var, command=self.eq_toggle,
                      progress_color=PURPLE).pack(side="left", padx=14)
        self.bass_var = tk.BooleanVar(value=self.bass_boost)
        ctk.CTkSwitch(top, text="Bass Boost", variable=self.bass_var, command=self.toggle_bass,
                      progress_color=PINK).pack(side="left", padx=8)
        self.vocal_var = tk.BooleanVar(value=self.vocal_boost)
        ctk.CTkSwitch(top, text="Vocal Boost", variable=self.vocal_var, command=self.toggle_vocal,
                      progress_color=CYAN).pack(side="left", padx=8)

        band_frame = ctk.CTkFrame(win, fg_color=PANEL, corner_radius=18, border_width=1, border_color=BORDER)
        band_frame.pack(fill="both", expand=True, padx=24, pady=8)
        self.eq_sliders = []
        self.eq_value_labels = []
        for i, freq in enumerate(EQ_FREQUENCIES):
            col = ctk.CTkFrame(band_frame, fg_color="transparent")
            col.grid(row=0, column=i, sticky="nsew", padx=4, pady=12)
            band_frame.grid_columnconfigure(i, weight=1)
            val = ctk.CTkLabel(col, text=f"{self.eq_gains[i]:+.0f} dB", text_color=PINK,
                               font=("Helvetica", 10, "bold"))
            val.pack(pady=(0, 5))
            slider = ctk.CTkSlider(col, from_=12, to=-12, number_of_steps=24, orientation="vertical",
                                   height=180, width=20, progress_color=PURPLE,
                                   button_color=PINK, button_hover_color=CYAN,
                                   command=lambda value, idx=i: self.eq_band_changed(idx, value))
            slider.set(self.eq_gains[i])
            slider.pack(pady=3)
            ctk.CTkLabel(col, text=(f"{freq//1000}k" if freq >= 1000 else str(freq)),
                         text_color=WHITE, font=("Helvetica", 10, "bold")).pack(pady=(5, 0))
            self.eq_sliders.append(slider)
            self.eq_value_labels.append(val)

        bottom = ctk.CTkFrame(win, fg_color="transparent")
        bottom.pack(fill="x", padx=24, pady=(4, 18))
        ctk.CTkLabel(bottom, text="Preamp", text_color=MUTED).pack(side="left")
        self.preamp_slider = ctk.CTkSlider(bottom, from_=-12, to=12, number_of_steps=24,
                                           width=150, progress_color=PURPLE,
                                           command=self.preamp_changed)
        self.preamp_slider.set(self.preamp_db)
        self.preamp_slider.pack(side="left", padx=8)
        self.preamp_label = ctk.CTkLabel(bottom, text=f"{self.preamp_db:+.0f} dB", text_color=WHITE, width=55)
        self.preamp_label.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(bottom, text="Master", text_color=MUTED).pack(side="left")
        self.volume_slider = ctk.CTkSlider(bottom, from_=0, to=100, number_of_steps=100,
                                           width=150, progress_color=PINK, command=self.volume_changed)
        self.volume_slider.set(self.master_volume * 100)
        self.volume_slider.pack(side="left", padx=8)
        self.volume_label = ctk.CTkLabel(bottom, text=f"{int(self.master_volume*100)}%", text_color=WHITE, width=45)
        self.volume_label.pack(side="left")
        self.make_button(bottom, "Reset", self.reset_equalizer, CARD, width=75).pack(side="right", padx=(8, 0))
        self.make_button(bottom, "Save", self.save_eq_settings, PURPLE, width=75, text_color="#151526").pack(side="right")
        win.protocol("WM_DELETE_WINDOW", self.close_equalizer)

    def close_equalizer(self):
        self.save_eq_settings()
        if getattr(self, "eq_window", None) is not None:
            self.eq_window.destroy()
        self.eq_window = None

    def eq_band_changed(self, index, value):
        self.eq_gains[index] = float(value)
        self.eq_value_labels[index].configure(text=f"{value:+.0f} dB")
        self.eq_preset = "Custom"
        self.audio.set_eq(gains=self.eq_gains)
        self.save_eq_settings()

    def apply_eq_preset(self, preset):
        self.eq_preset = preset
        self.eq_gains = list(EQ_PRESETS[preset])
        if self.bass_boost:
            self.eq_gains[0] = min(12, self.eq_gains[0] + 5)
            self.eq_gains[1] = min(12, self.eq_gains[1] + 4)
        if self.vocal_boost:
            self.eq_gains[4] = min(12, self.eq_gains[4] + 3)
            self.eq_gains[5] = min(12, self.eq_gains[5] + 4)
            self.eq_gains[6] = min(12, self.eq_gains[6] + 3)
        for slider, label, gain in zip(self.eq_sliders, self.eq_value_labels, self.eq_gains):
            slider.set(gain)
            label.configure(text=f"{gain:+.0f} dB")
        self.audio.set_eq(gains=self.eq_gains)
        self.save_eq_settings()

    def eq_toggle(self):
        self.eq_enabled = bool(self.eq_on_var.get())
        self.audio.set_eq(enabled=self.eq_enabled)
        self.save_eq_settings()

    def toggle_bass(self):
        self.bass_boost = bool(self.bass_var.get())
        self.apply_eq_preset(self.preset_var.get())

    def toggle_vocal(self):
        self.vocal_boost = bool(self.vocal_var.get())
        self.apply_eq_preset(self.preset_var.get())

    def preamp_changed(self, value):
        self.preamp_db = float(value)
        self.preamp_label.configure(text=f"{value:+.0f} dB")
        self.audio.set_eq(preamp=self.preamp_db)
        self.save_eq_settings()

    def volume_changed(self, value):
        self.master_volume = float(value) / 100.0
        self.volume_label.configure(text=f"{int(value)}%")
        self.audio.set_eq(volume=self.master_volume)
        self.save_eq_settings()

    def reset_equalizer(self):
        self.bass_boost = self.vocal_boost = False
        self.bass_var.set(False)
        self.vocal_var.set(False)
        self.apply_eq_preset("Flat")
        self.eq_on_var.set(True)
        self.eq_enabled = True
        self.audio.set_eq(enabled=True, preamp=0.0)
        self.preamp_db = 0.0
        self.preamp_slider.set(0)
        self.preamp_label.configure(text="+0 dB")
        self.save_eq_settings()


    def make_button(
        self,
        parent,
        text,
        command,
        color,
        width=100,
        text_color=WHITE
    ):
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=42,
            corner_radius=13,
            fg_color=color,
            hover_color="#B9A1FF" if color == PURPLE else "#383854",
            text_color=text_color,
            font=("Helvetica", 12, "bold")
        )

    def save_playlist(self):
        songs = [
            {
                "title": song.title,
                "artist": song.artist,
                "filepath": song.filepath
            }
            for song in self.playlist.get_all()
        ]

        try:
            # Keep the existing JSON file as a compatibility copy.
            with open(self.playlist_file, "w", encoding="utf-8") as f:
                json.dump(songs, f, ensure_ascii=False, indent=2)

            # Persist the current playlist to SQLite.
            with sqlite3.connect(self.database_file) as con:
                con.execute("PRAGMA foreign_keys = ON")
                con.execute("""
                    CREATE TABLE IF NOT EXISTS songs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        artist TEXT NOT NULL DEFAULT 'Unknown Artist',
                        filepath TEXT NOT NULL UNIQUE,
                        is_favorite INTEGER NOT NULL DEFAULT 0,
                        date_added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS playlists (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        date_created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS playlist_songs (
                        playlist_id INTEGER NOT NULL,
                        song_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        PRIMARY KEY (playlist_id, song_id),
                        FOREIGN KEY (playlist_id) REFERENCES playlists(id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (song_id) REFERENCES songs(id)
                            ON DELETE CASCADE
                    )
                """)

                con.execute(
                    "INSERT OR IGNORE INTO playlists (name) VALUES (?)",
                    ("My Playlist",)
                )
                playlist_id = con.execute(
                    "SELECT id FROM playlists WHERE name = ?",
                    ("My Playlist",)
                ).fetchone()[0]

                for position, song in enumerate(songs):
                    filepath = song["filepath"]
                    con.execute("""
                        INSERT INTO songs (title, artist, filepath)
                        VALUES (?, ?, ?)
                        ON CONFLICT(filepath) DO UPDATE SET
                            title = excluded.title,
                            artist = excluded.artist
                    """, (song["title"], song["artist"], filepath))

                con.execute(
                    "DELETE FROM playlist_songs WHERE playlist_id = ?",
                    (playlist_id,)
                )

                for position, song in enumerate(songs):
                    song_id = con.execute(
                        "SELECT id FROM songs WHERE filepath = ?",
                        (song["filepath"],)
                    ).fetchone()[0]
                    con.execute("""
                        INSERT INTO playlist_songs
                            (playlist_id, song_id, position)
                        VALUES (?, ?, ?)
                    """, (playlist_id, song_id, position))

        except Exception as e:
            messagebox.showerror("Playlist save error", str(e))

    def load_playlist(self):
        songs = None

        # Prefer SQLite when the database and playlist tables exist.
        if os.path.isfile(getattr(self, "database_file", "")):
            try:
                with sqlite3.connect(self.database_file) as con:
                    tables = {
                        row[0] for row in con.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }

                    if {"songs", "playlists", "playlist_songs"}.issubset(tables):
                        row = con.execute(
                            "SELECT id FROM playlists WHERE name = ?",
                            ("My Playlist",)
                        ).fetchone()

                        if row:
                            songs = [
                                {
                                    "title": item[0],
                                    "artist": item[1],
                                    "filepath": item[2]
                                }
                                for item in con.execute("""
                                    SELECT s.title, s.artist, s.filepath
                                    FROM playlist_songs ps
                                    JOIN songs s ON s.id = ps.song_id
                                    WHERE ps.playlist_id = ?
                                    ORDER BY ps.position
                                """, (row[0],))
                            ]
            except Exception as e:
                messagebox.showwarning(
                    "Database load warning",
                    f"Could not load the database playlist:\n{e}\n"
                    "Trying the JSON playlist instead."
                )

        # Fall back to JSON if no SQLite playlist is available.
        if songs is None:
            try:
                with open(self.playlist_file, "r", encoding="utf-8") as f:
                    songs = json.load(f)
            except FileNotFoundError:
                return
            except Exception as e:
                messagebox.showwarning("Playlist load error", str(e))
                return

        for song in songs:
            filepath = song.get("filepath", "")
            if filepath and os.path.isfile(filepath):
                self.playlist.add_song(
                    song.get(
                        "title",
                        os.path.splitext(os.path.basename(filepath))[0]
                    ),
                    song.get("artist", "Unknown Artist"),
                    filepath
                )

    def play_online_preview(self, item, filepath):
        try:
            self.audio.load(filepath, loop=False, autoplay=True)
            self.is_online_playback = True
            self.song_duration = self.audio.duration
            self.position_before_pause = 0.0
            self.is_playing = True
            self.play_button.configure(text="⏸")
            self.now_title.configure(text=item.get("title", "Online audio"))
            self.set_vinyl_title(item.get("title", "Online audio"))
            self.now_artist.configure(text=item.get("creator", item.get("source", "Online")))
            self.status_label.configure(text="Playing online audio")
            self.progress_slider.configure(from_=0, to=max(1, self.song_duration))
            self.progress_slider.set(0)
        except Exception as error:
            self.status_label.configure(text="Online playback failed")
            messagebox.showerror("Playback error", str(error))
        finally:
            # AudioEngine loads the WAV into memory, so the temporary conversion
            # file can be removed after load() returns.
            try:
                os.remove(filepath)
            except OSError:
                pass

    def open_online_music(self):
        """Open a compact search popup; suggestions appear after typing."""
        try:
            existing = getattr(self, "online_music_toplevel", None)
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                if self.online_music_window is not None:
                    self.online_music_window.entry.focus_set()
                return

            win = ctk.CTkToplevel(self)
            self.online_music_toplevel = win
            win.title("Music Bloom | Search Music")
            win.geometry("590x520")
            win.minsize(480, 400)
            win.configure(fg_color=BG)
            win.grid_columnconfigure(0, weight=1)
            win.grid_rowconfigure(0, weight=1)

            search_view = OnlineMusicWindow(win, self.add_online_track)
            self.online_music_window = search_view
            search_view.on_preview = self.play_online_preview
            search_view.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)

            def close_online_window():
                self.online_music_window = None
                self.online_music_toplevel = None
                win.destroy()

            win.protocol("WM_DELETE_WINDOW", close_online_window)
            win.after(150, search_view.entry.focus_set)
        except (AttributeError, tk.TclError) as error:
            messagebox.showerror("Online Music", f"Could not open Online Music: {error}")

if __name__ == "__main__":
    app = MusicBloomApp()
    app.mainloop()
