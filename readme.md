# 📦 Bunkr Downloader

A fast, asynchronous Bunkr downloader built with Python.

Download **entire albums** or **individual files** with a modern live terminal UI, concurrent downloads, automatic CDN URL resolution, filtering by media type, and resumable output management.

---

## ✨ Features

* ⚡ Fully asynchronous (`asyncio` + `aiohttp`)
* 📁 Download complete Bunkr albums
* 📄 Download individual file pages
* 🎥 Filter videos only
* 🖼️ Filter images only
* 📦 Filter archives/files only
* 📊 Beautiful live progress table powered by Rich
* 🚀 Concurrent URL resolving
* 🚀 Concurrent downloading
* 🧹 Automatic filename & folder sanitization
* 🔄 Resolves signed CDN download URLs automatically
* 📂 Organized album folders
* ✅ Skips files that already exist
* 📈 Live download speeds
* 📉 Progress percentages
* 📝 Download summary
* 🛡️ Robust error handling

---

## 📦 Requirements

* Python **3.10+**

Install dependencies:

```bash
pip install aiohttp beautifulsoup4 rich lxml
```

---

## 🚀 Usage

Download an entire album:

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID
```

Download videos only:

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID -v
```

Download images only:

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID -i
```

Download files only (.zip, .rar, etc.):

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID -f
```

Download a single file page:

```bash
python bunkdl.py https://bunkr.cr/f/FILE_ID
```

Choose a custom output directory:

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID -o ./downloads
```

Increase concurrent downloads:

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID -j 4
```

Increase concurrent URL resolving:

```bash
python bunkdl.py https://bunkr.cr/a/ALBUM_ID -r 10
```

---

## ⚙️ Command Line Options

| Option | Description                        |
| ------ | ---------------------------------- |
| `-a`   | Download all media (default)       |
| `-v`   | Videos only                        |
| `-i`   | Images only                        |
| `-f`   | Other files only                   |
| `-o`   | Output directory                   |
| `-j`   | Number of concurrent downloads     |
| `-r`   | Number of concurrent URL resolvers |

---

## 📂 Output

Downloads are automatically organized into album folders:

```
bunkr_downloads/
└── Album Name/
    ├── image1.jpg
    ├── image2.png
    ├── video1.mp4
    └── archive.zip
```

---

## 📺 Live Terminal Interface

The downloader provides a real-time dashboard showing:

* Download progress
* Download speed
* Current status
* File size
* Queue progress
* Completed downloads
* Failed downloads
* Skipped files

---

## 🛠 Built With

* Python
* asyncio
* aiohttp
* BeautifulSoup4
* Rich

---

## ⚠️ Disclaimer

This project is intended for educational purposes and downloading content you have permission to access. Users are responsible for complying with applicable laws and the terms of service of any websites they use.

---

## ⭐ Contributing

Pull requests, bug reports, and feature suggestions are welcome.

If you find this project useful, consider giving it a ⭐ on GitHub.
