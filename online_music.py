import json
import os
import threading
import urllib.parse
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

ARCHIVE_API = "https://archive.org/advancedsearch.php"
ITUNES_API = "https://itunes.apple.com/search"
AUDIUS_API = "https://api.audius.co/v1"
USER_AGENT = "MusicBloom/2.0"
SUPPORTED_FORMATS = (".mp3", ".ogg", ".wav", ".flac")
MAX_BYTES = 100 * 1024 * 1024


class OnlineMusicWindow(ctk.CTkFrame):
    """Search Apple song previews and Internet Archive audio."""

    def __init__(self, master, on_downloaded):
        super().__init__(master)

        self.on_downloaded = on_downloaded
        self.results = []
        self.busy = False

        self.cache_dir = os.path.join(
            os.path.expanduser("~/Library/Application Support/Music Bloom"),
            "online_music_cache",
        )
        os.makedirs(self.cache_dir, exist_ok=True)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        heading = ctk.CTkLabel(
            self,
            text="Search Music",
            font=("Helvetica", 21, "bold"),
        )
        heading.grid(row=0, column=0, padx=20, pady=(20, 12))

        search_frame = ctk.CTkFrame(self, fg_color="transparent")
        search_frame.grid(
            row=1, column=0, padx=16, pady=8, sticky="ew"
        )
        search_frame.grid_columnconfigure(0, weight=1)

        self.query = tk.StringVar()

        self.entry = ctk.CTkEntry(
            search_frame,
            textvariable=self.query,
            placeholder_text="Search song, artist, or music...",
            height=40,
        )
        self.entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.entry.bind("<Return>", lambda event: self.search())
        self._suggestion_after = None
        self._last_auto_query = ""
        self.entry.bind("<KeyRelease>", self._schedule_suggestions)

        self.search_button = ctk.CTkButton(
            search_frame,
            text="🔎 Search",
            width=105,
            height=40,
            command=self.search,
        )
        self.search_button.grid(row=0, column=1)

        self.list_frame = ctk.CTkScrollableFrame(self)
        self.list_frame.grid(
            row=2, column=0, padx=16, pady=8, sticky="nsew"
        )
        self.list_frame.grid_columnconfigure(0, weight=1)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, padx=16, pady=(5, 15), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.status = ctk.CTkLabel(
            bottom,
            text="Type at least 3 characters for song suggestions, or press Search.",
            anchor="w",
            wraplength=700,
        )
        self.status.grid(row=0, column=0, sticky="ew")

        note = ctk.CTkLabel(
            bottom,
            text=(
                "Apple provides short previews when available. "
                "Internet Archive downloads vary; check each item's "
                "license and usage terms."
            ),
            font=("Helvetica", 10),
            wraplength=700,
            justify="left",
        )
        note.grid(row=1, column=0, pady=(6, 0), sticky="w")

        self.entry.focus_set()

    def set_status(self, text):
        self.status.configure(text=text)

    def _schedule_suggestions(self, event=None):
        """Debounce typing so results act like suggestions without opening a large window."""
        if self._suggestion_after is not None:
            try:
                self.after_cancel(self._suggestion_after)
            except tk.TclError:
                pass
        self._suggestion_after = self.after(650, self._auto_suggest)

    def _auto_suggest(self):
        self._suggestion_after = None
        query = self.query.get().strip()
        if len(query) < 3 or query == self._last_auto_query or self.busy:
            return
        self._last_auto_query = query
        self.search()

    @staticmethod
    def _request_json(url, timeout=20):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def search(self):
        query = self.query.get().strip()

        if not query:
            self.set_status("Pehle song ya artist ka naam likho.")
            return

        if self.busy:
            return

        self.busy = True
        self.search_button.configure(state="disabled")
        self.set_status("Apple aur Internet Archive par search ho raha hai...")

        threading.Thread(
            target=self._search_worker,
            args=(query,),
            daemon=True,
        ).start()

    def _search_worker(self, query):
        found = []
        errors = []

        # Apple song search: previews are short clips, not full songs.
        try:
            params = urllib.parse.urlencode({
                "term": query,
                "media": "music",
                "entity": "song",
                "limit": 25,
            })
            data = self._request_json(ITUNES_API + "?" + params)

            for item in data.get("results", []):
                title = item.get("trackName")
                artist = item.get("artistName")

                if not title or not artist:
                    continue

                found.append({
                    "source": "Apple preview",
                    "title": title,
                    "creator": artist,
                    "album": item.get("collectionName", "Album not listed"),
                    "preview_url": item.get("previewUrl", ""),
                    "track_url": item.get("trackViewUrl", ""),
                    "license": "Apple preview; see Apple listing",
                })

        except Exception as error:
            errors.append("Apple: " + str(error))

        # Audius search integration
        try:
            params = urllib.parse.urlencode({
                "query": query,
                "limit": 20,
            })
            data = self._request_json(
                AUDIUS_API + "/tracks/search?" + params,
                timeout=20,
            )

            for track in data.get("data", []):
                track_id = track.get("id")
                if not track_id:
                    continue

                user = track.get("user") or {}
                found.append({
                    "source": "Audius",
                    "title": track.get("title") or "Untitled track",
                    "creator": user.get("name") or "Unknown artist",
                    "track_id": str(track_id),
                    "license": "Check track usage terms",
                })

        except Exception as error:
            errors.append("Audius: " + str(error))

        # Internet Archive audio search.
        try:
            escaped = query.replace("\\", "\\\\").replace('"', '\\"')
            params = {
                "q": f'mediatype:audio AND "{escaped}"',
                "fl[]": ["identifier", "title", "creator", "licenseurl"],
                "rows": 25,
                "output": "json",
            }
            url = ARCHIVE_API + "?" + urllib.parse.urlencode(
                params, doseq=True
            )
            data = self._request_json(url, timeout=25)
            docs = data.get("response", {}).get("docs", [])

            for doc in docs:
                identifier = doc.get("identifier", "")
                if not identifier:
                    continue

                metadata_url = (
                    "https://archive.org/metadata/"
                    + urllib.parse.quote(identifier, safe="")
                )

                try:
                    metadata = self._request_json(metadata_url, timeout=15)
                except Exception:
                    continue

                audio_files = []

                for file_info in metadata.get("files", []):
                    name = file_info.get("name", "")
                    size = file_info.get("size", "0")

                    if (
                        name.lower().endswith(SUPPORTED_FORMATS)
                        and file_info.get("private") != "true"
                        and str(size).isdigit()
                        and 10_000 < int(size) <= MAX_BYTES
                    ):
                        audio_files.append(file_info)

                if not audio_files:
                    continue

                audio_files.sort(
                    key=lambda item: (
                        not item.get("name", "").lower().endswith(".mp3"),
                        int(item.get("size", 0)),
                    )
                )
                chosen = audio_files[0]

                found.append({
                    "source": "Internet Archive",
                    "title": doc.get("title") or identifier,
                    "creator": doc.get("creator") or "Creator not listed",
                    "identifier": identifier,
                    "license": doc.get("licenseurl") or "Not specified",
                    "filename": chosen["name"],
                    "size": int(chosen["size"]),
                    "item_url": (
                        "https://archive.org/details/"
                        + urllib.parse.quote(identifier, safe="")
                    ),
                })

        except Exception as error:
            errors.append("Internet Archive: " + str(error))

        self.after(
            0,
            lambda results=found, problems=errors:
                self._show_results(results, problems),
        )

    def _show_results(self, found, errors):
        self.busy = False
        self.search_button.configure(state="normal")
        self.results = found

        for widget in self.list_frame.winfo_children():
            widget.destroy()

        if not found:
            message = "Koi result nahi mila. Doosra search term try karo."
            if errors:
                message += " Errors: " + "; ".join(errors)
            self.set_status(message)
            return

        counts = {
            "Apple preview": 0,
            "Audius": 0,
            "Internet Archive": 0,
        }
        for item in found:
            source = item.get("source", "Unknown")
            counts[source] = counts.get(source, 0) + 1

        message = (
            f"Found {len(found)} results — "
            f"Apple: {counts['Apple preview']}, "
            f"Audius: {counts['Audius']}, "
            f"Internet Archive: {counts['Internet Archive']}."
        )
        if errors:
            message += " Some sources could not be reached."
        self.set_status(message)

        for index, item in enumerate(found):
            card = ctk.CTkFrame(self.list_frame)
            card.grid(
                row=index, column=0, padx=4, pady=5, sticky="ew"
            )
            card.grid_columnconfigure(0, weight=1)

            source = item.get("source", "Unknown")
            title = item.get("title") or "Untitled track"
            creator = item.get("creator") or "Unknown artist"

            ctk.CTkLabel(
                card,
                text=title,
                font=("Helvetica", 14, "bold"),
                anchor="w",
                wraplength=490,
                justify="left",
            ).grid(
                row=0, column=0, padx=12, pady=(9, 3), sticky="ew"
            )

            if source == "Apple preview":
                details = (
                    f"Apple preview • {creator}\n"
                    f"{item.get('album', 'Album not listed')}"
                )
                command = lambda selected=item: self.open_apple(selected)

            elif source == "Audius":
                details = (
                    f"Audius • {creator}\n"
                    "Full-track availability depends on the source."
                )
                command = lambda selected=item: self.play_audius(selected)

            elif source == "Internet Archive":
                size_mb = item.get("size", 0) / (1024 * 1024)
                details = (
                    f"Internet Archive • {creator}\n"
                    f"{size_mb:.1f} MB • "
                    f"License: {item.get('license', 'Not specified')}"
                )
                command = lambda selected=item: self.play_archive(selected)

            else:
                details = source
                command = None

            ctk.CTkLabel(
                card,
                text=details,
                anchor="w",
                justify="left",
                wraplength=490,
                font=("Helvetica", 11),
            ).grid(
                row=1, column=0, padx=12, pady=(0, 9), sticky="ew"
            )

            button = ctk.CTkButton(
                card,
                text="▶ Play",
                width=135,
                command=command if command else lambda: None,
                state="normal" if command else "disabled",
            )
            button.grid(
                row=0, column=1, rowspan=2, padx=12, pady=10
            )

    def open_apple(self, item):
        url = item.get("preview_url")
        if not url:
            self.set_status("Is song ka playable preview available nahi hai.")
            return

        callback = getattr(self, "on_preview", None)
        if not callable(callback):
            self.set_status("In-app preview callback configured nahi hai.")
            return

        self.set_status("Preview load ho raha hai...")
        threading.Thread(
            target=self._preview_worker,
            args=(item, url, callback),
            daemon=True,
        ).start()

    def _preview_worker(self, item, url, callback):
        import subprocess
        import tempfile

        wav_path = None
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                audio_bytes = response.read(20 * 1024 * 1024 + 1)

            if not audio_bytes or len(audio_bytes) > 20 * 1024 * 1024:
                raise RuntimeError("Preview empty hai ya allowed size se bada hai.")

            fd, source_path = tempfile.mkstemp(suffix=".m4a")
            os.close(fd)
            with open(source_path, "wb") as f:
                f.write(audio_bytes)

            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            result = subprocess.run(
                [
                    "/opt/homebrew/bin/ffmpeg", "-y", "-v", "error",
                    "-i", source_path,
                    "-t", "60",
                    "-ac", "2",
                    "-ar", "44100",
                    "-c:a", "pcm_s16le",
                    wav_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            try:
                os.remove(source_path)
            except OSError:
                pass

            if result.returncode != 0 or not os.path.isfile(wav_path):
                raise RuntimeError(result.stderr.strip() or "Audio conversion failed.")

            self.after(
                0,
                lambda path=wav_path, song=item: self._start_preview(path, song, callback),
            )
        except Exception as error:
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            self.after(0, lambda msg=str(error): self.set_status("Preview failed: " + msg))

    def _start_preview(self, wav_path, item, callback):
        try:
            callback(item, wav_path)
            self.set_status("Audio Music Bloom ke andar play ho raha hai.")
        except Exception as error:
            try:
                os.remove(wav_path)
            except OSError:
                pass
            self.set_status("Preview playback failed: " + str(error))


    def play_audius(self, item):
        callback = getattr(self, "on_preview", None)
        if not callback:
            self.set_status("ERROR: Main player callback connect nahi hai.")
            return

        track_id = item.get("track_id")
        if not track_id:
            self.set_status("ERROR: Audius track ID nahi mila.")
            return

        if self.busy:
            self.set_status("Ek audio operation pehle se chal raha hai.")
            return

        self.busy = True
        self.search_button.configure(state="disabled")
        self.set_status("Audius track download aur convert ho raha hai...")

        threading.Thread(
            target=self._audius_play_worker,
            args=(item, callback),
            daemon=True,
        ).start()

    def _audius_play_worker(self, item, callback):
        import subprocess

        track_id = str(item.get("track_id", ""))
        safe_id = "".join(c for c in track_id if c.isalnum() or c in "-_")
        destination = os.path.join(self.cache_dir, "audius_" + safe_id + ".audio")
        wav_path = os.path.join(self.cache_dir, "audius_" + safe_id + ".wav")
        partial = destination + ".part"

        try:
            url = (
                AUDIUS_API + "/tracks/" + urllib.parse.quote(track_id)
                + "/stream?" + urllib.parse.urlencode({"app_name": "MusicBloom"})
            )
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT}
            )

            if not os.path.exists(destination) or os.path.getsize(destination) == 0:
                total = 0
                with urllib.request.urlopen(request, timeout=40) as response:
                    with open(partial, "wb") as output:
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_BYTES:
                                raise RuntimeError("Audio 100 MB se bada hai.")
                            output.write(chunk)

                if total == 0:
                    raise RuntimeError("Audius ne khaali audio stream bheji.")
                os.replace(partial, destination)

            result = subprocess.run(
                [
                    "/opt/homebrew/bin/ffmpeg", "-y", "-v", "error",
                    "-i", destination,
                    "-ac", "2", "-ar", "44100",
                    "-c:a", "pcm_s16le", wav_path,
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )

            if result.returncode != 0 or not os.path.exists(wav_path):
                detail = (result.stderr or "Unknown ffmpeg error").strip()
                raise RuntimeError("Audio conversion failed: " + detail[-500:])

            self.after(
                0,
                lambda path=wav_path, selected=item, cb=callback:
                    self._archive_ready(path, selected, cb),
            )

        except Exception as exc:
            for path in (partial, wav_path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

            self.after(
                0,
                lambda error=str(exc): self._archive_failed("Audius: " + error),
            )

    def play_archive(self, item):
        callback = getattr(self, "on_preview", None)
        if not callable(callback):
            self.set_status("In-app playback connection configured nahi hai.")
            return
        if self.busy:
            self.set_status("Pehle chal raha search/download complete hone do.")
            return

        filename = os.path.basename(item.get("filename", ""))
        identifier = item.get("identifier", "")
        if not filename or not identifier:
            self.set_status("Internet Archive result ki file details incomplete hain.")
            return

        safe_id = "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in identifier
        )
        destination = os.path.join(self.cache_dir, safe_id + "_" + filename)

        self.busy = True
        self.search_button.configure(state="disabled")
        self.set_status("Internet Archive audio tayyar ho raha hai...")

        threading.Thread(
            target=self._archive_play_worker,
            args=(item, destination, callback),
            daemon=True,
        ).start()

    def _archive_play_worker(self, item, destination, callback):
        import subprocess
        import tempfile

        wav_path = None
        temporary = destination + ".part"

        try:
            # Use a complete cached download if available; otherwise download it.
            if not os.path.isfile(destination) or os.path.getsize(destination) < 10000:
                url = (
                    "https://archive.org/download/"
                    + urllib.parse.quote(item["identifier"], safe="")
                    + "/"
                    + urllib.parse.quote(item["filename"], safe="/")
                )
                request = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT}
                )
                total = 0
                with urllib.request.urlopen(request, timeout=40) as response:
                    with open(temporary, "wb") as output:
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_BYTES:
                                raise ValueError("Audio file 100 MB limit se badi hai.")
                            output.write(chunk)

                if total < 10000:
                    raise ValueError("Downloaded audio file bahut chhoti hai.")
                os.replace(temporary, destination)

            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            result = subprocess.run(
                [
                    "/opt/homebrew/bin/ffmpeg", "-y", "-v", "error",
                    "-i", destination,
                    "-ac", "2", "-ar", "44100",
                    "-c:a", "pcm_s16le",
                    wav_path,
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0 or not os.path.isfile(wav_path):
                raise RuntimeError(
                    result.stderr.strip() or "Audio conversion failed."
                )

            self.after(
                0,
                lambda path=wav_path, song=item:
                    self._archive_ready(path, song, callback),
            )
            wav_path = None  # The UI callback now owns this temporary WAV.

        except Exception as error:
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
            self.after(
                0,
                lambda msg=str(error): self._archive_failed(msg),
            )

    def _archive_ready(self, wav_path, item, callback):
        self.busy = False
        try:
            self.search_button.configure(state="normal")
        except Exception:
            pass
        self._start_preview(wav_path, item, callback)

    def _archive_failed(self, error):
        self.busy = False
        try:
            self.search_button.configure(state="normal")
        except Exception:
            pass
        self.set_status("Internet Archive playback failed: " + error)

    def download(self, item):
        if item.get("source") != "Internet Archive":
            self.set_status("Sirf Internet Archive audio download kiya ja sakta hai.")
            return

        if self.busy:
            return

        filename = os.path.basename(item.get("filename", ""))
        if not filename or filename in (".", ".."):
            self.set_status("Invalid filename.")
            return

        safe_id = "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in item["identifier"]
        )
        destination = os.path.join(
            self.cache_dir, safe_id + "_" + filename
        )

        if os.path.isfile(destination) and os.path.getsize(destination) > 0:
            try:
                self.on_downloaded(item, destination)
                self.set_status("Cached track library mein add ho gaya.")
            except Exception as error:
                self.set_status("Track add nahi hua: " + str(error))
            return

        confirmed = messagebox.askyesno(
            "Download audio",
            (
                f'Download "{item["title"]}" '
                f'({item["size"] / (1024 * 1024):.1f} MB)?\n\n'
                f'License: {item["license"]}\n\n'
                "Please check the item page and its usage terms."
            ),
            parent=self,
        )

        if not confirmed:
            return

        self.busy = True
        self.search_button.configure(state="disabled")
        self.set_status("Audio download ho raha hai...")

        threading.Thread(
            target=self._download_worker,
            args=(item, destination),
            daemon=True,
        ).start()

    def _download_worker(self, item, destination):
        url = (
            "https://archive.org/download/"
            + urllib.parse.quote(item["identifier"], safe="")
            + "/"
            + urllib.parse.quote(item["filename"], safe="/")
        )
        temporary = destination + ".part"

        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            total = 0

            with urllib.request.urlopen(request, timeout=30) as response:
                with open(temporary, "wb") as output:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break

                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError(
                                "Download exceeded the 100 MB limit."
                            )

                        output.write(chunk)

            if total < 10_000:
                raise ValueError(
                    "Downloaded file is too small to be valid audio."
                )

            os.replace(temporary, destination)

            self.after(
                0,
                lambda: self._download_finished(item, destination),
            )

        except Exception as error:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

            self.after(
                0,
                lambda message=str(error): self._download_failed(message),
            )

    def _download_finished(self, item, destination):
        self.busy = False
        self.search_button.configure(state="normal")
        self.set_status("Download complete. Track add ho raha hai...")

        try:
            self.on_downloaded(item, destination)
            self.set_status("Track tumhari library mein add ho gaya.")
        except Exception as error:
            self.set_status(
                "Download ho gaya, lekin track add nahi hua: " + str(error)
            )

    def _download_failed(self, error):
        self.busy = False
        self.search_button.configure(state="normal")
        self.set_status("Download failed: " + error)