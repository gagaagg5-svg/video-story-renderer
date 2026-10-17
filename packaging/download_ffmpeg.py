"""Fetch the Windows FFmpeg essentials build and verify its published SHA256."""
import hashlib
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
root = Path(__file__).resolve().parents[1]
target = root / 'vendor' / 'ffmpeg'
target.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as work:
    archive = Path(work) / 'ffmpeg.zip'
    urllib.request.urlretrieve(URL, archive)
    with urllib.request.urlopen(URL + '.sha256', timeout=60) as response:
        text = response.read().decode('utf-8')
    match = re.search(r'\b[0-9a-fA-F]{64}\b', text)
    if not match:
        raise RuntimeError('Cannot read FFmpeg checksum')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != match.group(0).lower():
        raise RuntimeError('FFmpeg SHA256 mismatch; retry if the upstream release changed')
    with zipfile.ZipFile(archive) as z:
        for filename in ('ffmpeg.exe', 'ffprobe.exe', 'LICENSE', 'README.txt'):
            matches = [n for n in z.namelist() if not n.endswith('/') and Path(n).name == filename]
            if len(matches) != 1:
                raise RuntimeError(f'Missing or ambiguous file in archive: {filename}')
            with z.open(matches[0]) as src, (target / filename).open('wb') as dst:
                shutil.copyfileobj(src, dst)
    (target / 'SOURCE_INFO.txt').write_text(
        f'Binary source: {URL}\nSHA256: {digest}\n'
        'Build details and source information: https://www.gyan.dev/ffmpeg/builds/\n'
        'FFmpeg project and source releases: https://ffmpeg.org/download.html\n', encoding='utf-8')
print('FFmpeg and ffprobe downloaded and verified.')
