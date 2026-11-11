from pathlib import Path
from urllib.parse import urlparse, unquote
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import hmac
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
MAX_FILE_SIZE = 150 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 180
PASSWORD = os.environ.get('PINDOWN_PASSWORD', '')
SESSION_COOKIE = 'pindown_session'
SESSION_MAX_AGE = 30 * 24 * 60 * 60
logger = logging.getLogger('pindown')
download_slots = BoundedSemaphore(2)
rate_lock = Lock()
rate_windows = defaultdict(deque)

class Link(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

class PasswordEntry(BaseModel):
    password: str = Field(min_length=1, max_length=256)

PLATFORMS = {
    'pinterest.com': ('Pinterest', True),
    'pin.it': ('Pinterest', False),
    'www.pin.it': ('Pinterest', False),
    # Outros extratores continuam disponíveis sem aparecer como foco na interface.
    'instagram.com': ('Instagram', True),
    'youtube.com': ('YouTube', True),
    'youtu.be': ('YouTube', False),
    'facebook.com': ('Facebook', True),
    'fb.watch': ('Facebook', False),
}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    environment = os.environ.get('APP_ENV', 'production').lower()
    if environment not in {'development', 'test'} and len(PASSWORD) < 12:
        raise RuntimeError('Configure PINDOWN_PASSWORD com pelo menos 12 caracteres antes de expor o app.')
    if PASSWORD and len(PASSWORD) < 12:
        raise RuntimeError('PINDOWN_PASSWORD precisa ter pelo menos 12 caracteres.')
    logger.info('%s starting in %s mode', _app.title, environment)
    yield

app = FastAPI(
    title='PinDown',
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount('/static', StaticFiles(directory=STATIC), name='static')

def validate(url: str) -> tuple[str, str]:
    url = url.strip()
    parsed = None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        has_port = parsed.port is not None
    except ValueError:
        host = ''
        has_port = True

    platform = next(
        (
            name
            for domain, (name, allow_subdomains) in PLATFORMS.items()
            if host == domain or (allow_subdomains and host.endswith(f'.{domain}'))
        ),
        None,
    )
    if (
        parsed is None
        or parsed.scheme.lower() != 'https'
        or not platform
        or parsed.username is not None
        or parsed.password is not None
        or has_port
    ):
        raise HTTPException(
            400,
            'Cole um link HTTPS válido de um Pin público do Pinterest.',
        )
    return url, platform

def check_rate_limit(request: Request, endpoint: str, limit: int) -> None:
    client = request.client.host if request.client else 'unknown'
    key = (client, endpoint)
    now = monotonic()
    with rate_lock:
        for old_key, timestamps in list(rate_windows.items()):
            while timestamps and now - timestamps[0] >= 60:
                timestamps.popleft()
            if not timestamps:
                del rate_windows[old_key]
        if len(rate_windows) >= 10_000 and key not in rate_windows:
            raise HTTPException(503, 'Servidor ocupado. Tente novamente em instantes.')
        timestamps = rate_windows[key]
        if len(timestamps) >= limit:
            raise HTTPException(429, 'Muitas solicitações. Aguarde um minuto e tente novamente.')
        timestamps.append(now)

def create_session_token() -> str:
    expires_at = str(int(time.time()) + SESSION_MAX_AGE)
    signature = hmac.new(
        PASSWORD.encode('utf-8'),
        f'pindown-session:{expires_at}'.encode('ascii'),
        'sha256',
    ).hexdigest()
    return f'{expires_at}.{signature}'

def has_valid_session(request: Request) -> bool:
    if not PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, '')
    try:
        expires_at, signature = token.split('.', 1)
        if int(expires_at) <= int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        PASSWORD.encode('utf-8'),
        f'pindown-session:{expires_at}'.encode('ascii'),
        'sha256',
    ).hexdigest()
    return hmac.compare_digest(signature, expected)

def require_password(request: Request) -> None:
    if not has_valid_session(request):
        raise HTTPException(401, 'Sua sessão expirou. Entre novamente.')

@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.get('/api/auth/status')
def auth_status(request: Request):
    return {'authenticated': has_valid_session(request)}

@app.post('/api/auth/login')
def login(
    entry: PasswordEntry,
    request: Request,
    response: Response,
):
    check_rate_limit(request, 'login', 5)
    if not PASSWORD or not hmac.compare_digest(
        entry.password.encode('utf-8'),
        PASSWORD.encode('utf-8'),
    ):
        raise HTTPException(401, 'Senha incorreta.')
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=os.environ.get('APP_ENV', 'production').lower() not in {'development', 'test'},
        samesite='strict',
        path='/',
    )
    return {'authenticated': True}

@app.post('/api/auth/logout')
def logout(response: Response):
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=os.environ.get('APP_ENV', 'production').lower() not in {'development', 'test'},
        samesite='strict',
        path='/',
    )
    return {'authenticated': not PASSWORD}

@app.middleware('http')
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.post('/api/info', dependencies=[Depends(require_password)])
def info(
    link: Link,
    request: Request,
):
    check_rate_limit(request, 'info', 20)
    url, platform = validate(link.url)
    try:
        with YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 20,
            'retries': 1,
        }) as ydl:
            data = ydl.extract_info(url, download=False)
        if not data:
            raise ValueError('Nenhum vídeo encontrado')
        return {
            'title': data.get('title') or f'Vídeo do {platform}',
            'thumbnail': data.get('thumbnail'),
            'duration': data.get('duration'),
            'platform': platform,
        }
    except Exception as exc:
        logger.warning('Preview extraction failed for %s (%s)', platform, type(exc).__name__)
        raise HTTPException(
            422,
            f'Não foi possível obter a prévia. Verifique se o conteúdo do {platform} é público e contém vídeo.',
        ) from exc

@app.post('/api/download', dependencies=[Depends(require_password)])
def download(
    link: Link,
    request: Request,
):
    check_rate_limit(request, 'download', 5)
    url, platform = validate(link.url)
    if not download_slots.acquire(blocking=False):
        raise HTTPException(429, 'Já existem downloads em andamento. Tente novamente em instantes.')

    folder = None
    handed_off = False
    try:
        folder = Path(tempfile.mkdtemp(prefix='pindown_'))
        started_at = monotonic()
        downloaded_sizes = {}

        def enforce_download_limits(status):
            if monotonic() - started_at > MAX_DOWNLOAD_SECONDS:
                raise DownloadError('Download excedeu o tempo máximo permitido.')
            filename = status.get('filename')
            if filename and status.get('status') in {'downloading', 'finished'}:
                downloaded_sizes[filename] = status.get('downloaded_bytes') or 0
                if sum(downloaded_sizes.values()) > MAX_FILE_SIZE:
                    raise DownloadError('Download excedeu o limite de tamanho permitido.')

        options = {
            'quiet': True,
            'no_warnings': True,
            # Preferir vídeo quando houver, com fallback para imagens/GIFs.
            'format': 'bestvideo+bestaudio/best/bestimage/best',
            'merge_output_format': 'mp4',
            'outtmpl': str(folder / '%(id)s.%(ext)s'),
            'noplaylist': True,
            'max_filesize': MAX_FILE_SIZE,
            'socket_timeout': 20,
            'retries': 2,
            'fragment_retries': 2,
            'progress_hooks': [enforce_download_limits],
        }

        with YoutubeDL(options) as ydl:
            ydl.download([url])

        # Localiza apenas o arquivo final
        files = [
            f for f in folder.iterdir()
            if f.is_file()
            and not f.name.endswith(('.part', '.ytdl'))
        ]

        if not files:
            raise ValueError('Arquivo não encontrado')

        source = max(files, key=lambda f: f.stat().st_size)

        if source.stat().st_size > MAX_FILE_SIZE:
            raise ValueError(f'O arquivo excede o limite de {MAX_FILE_SIZE // (1024 * 1024)} MB.')

        from starlette.background import BackgroundTask

        response = FileResponse(
            source,
            filename=f'pindown-{uuid.uuid4().hex[:8]}{source.suffix}',
            media_type='application/octet-stream',
            background=BackgroundTask(cleanup_download, folder),
        )
        handed_off = True
        return response

    except Exception as exc:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        logger.warning('Download failed for %s (%s)', platform, type(exc).__name__)
        raise HTTPException(422, f'Falha ao baixar. Confira se o conteúdo do {platform} é público e tente novamente.') from exc
    finally:
        if not handed_off:
            download_slots.release()

# ---------------------------------------------------------------------------
# Pastas (boards) do Pinterest
# ---------------------------------------------------------------------------
MAX_BOARD_PINS = int(os.environ.get('PINDOWN_MAX_BOARD_PINS', '40'))
MAX_BOARD_BYTES = int(os.environ.get('PINDOWN_MAX_BOARD_MB', '400')) * 1024 * 1024
MAX_BOARD_SECONDS = 900
MAX_PIN_SECONDS = 120
JOB_TTL_SECONDS = 900
KEEP_FINISHED_JOBS = 3
RESERVED_PINTEREST_PATHS = {
    'pin', 'ideas', 'search', 'topics', 'today', 'explore', 'settings',
    'business', 'news_hub', 'videos', 'shop', 'login', 'about',
}
board_slots = BoundedSemaphore(1)
jobs: dict[str, dict] = {}
jobs_lock = Lock()

def validate_board(url: str) -> tuple[str, str]:
    """Aceita só links de pasta pública: https://<sub>.pinterest.com/usuario/pasta/"""
    bad = HTTPException(400, 'Use o link de uma pasta pública do Pinterest (ex.: pinterest.com/usuario/pasta/).')
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or '').lower().rstrip('.')
        has_port = parsed.port is not None
    except ValueError:
        raise bad
    segments = [s for s in parsed.path.split('/') if s]
    if (
        parsed.scheme.lower() != 'https'
        or not (host == 'pinterest.com' or host.endswith('.pinterest.com'))
        or parsed.username is not None
        or parsed.password is not None
        or has_port
        or len(segments) != 2
        or unquote(segments[0]).lower() in RESERVED_PINTEREST_PATHS
    ):
        raise bad
    slug = re.sub(r'[^A-Za-z0-9_-]', '', unquote(segments[1]))[:40] or 'pasta'
    return f'https://{host}/{segments[0]}/{segments[1]}/', slug

def list_board(url: str, limit: int) -> tuple[str, list]:
    """Lista os pins da pasta sem baixar nada (extração 'flat')."""
    with YoutubeDL({
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        # Pinterest atualmente falha ao extrair alguns Pins de imagem/GIF como
        # vídeo. Não expanda cada Pin durante a leitura da pasta.
        'extract_flat': True,
        'ignoreerrors': True,
        'playlistend': limit + 1,
        'socket_timeout': 20,
        'retries': 1,
    }) as ydl:
        data = ydl.extract_info(url, download=False)
    if not data:
        raise ValueError('Pasta vazia')
    pins = [e for e in (data.get('entries') or []) if e]
    if not pins:
        raise ValueError('Nenhum pin encontrado')
    return data.get('title') or 'Pasta do Pinterest', pins

def purge_jobs() -> None:
    now = monotonic()
    with jobs_lock:
        finished = sorted(
            ((jid, j) for jid, j in jobs.items() if j['status'] != 'running'),
            key=lambda item: item[1]['finished'],
        )
        drop = {jid for jid, j in finished if now - j['finished'] > JOB_TTL_SECONDS}
        drop |= {jid for jid, _ in finished[:-KEEP_FINISHED_JOBS]} if KEEP_FINISHED_JOBS else {jid for jid, _ in finished}
        removed = [jobs.pop(jid) for jid in drop if jid in jobs]
    for job in removed:
        shutil.rmtree(job['folder'], ignore_errors=True)

def folder_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.iterdir() if f.is_file())

def run_board_job(job_id: str, board_url: str, slug: str) -> None:
    job = jobs[job_id]
    folder: Path = job['folder']
    media = folder / 'media'
    try:
        media.mkdir()
        started_at = monotonic()
        file_started: dict[str, float] = {}
        file_bytes: dict[str, int] = {}
        stopped = False

        def hook(status):
            nonlocal stopped
            now = monotonic()
            info = status.get('info_dict') or {}
            total = info.get('n_entries') or info.get('playlist_count')
            if total:
                job['total'] = min(int(total), MAX_BOARD_PINS)
            if info.get('playlist_index'):
                job['done'] = int(info['playlist_index'])
            filename = status.get('filename') or ''
            file_started.setdefault(filename, now)
            file_bytes[filename] = status.get('downloaded_bytes') or 0
            if now - started_at > MAX_BOARD_SECONDS or sum(file_bytes.values()) > MAX_BOARD_BYTES:
                stopped = True
            if stopped:
                job['note'] = 'Limite de tempo/tamanho atingido; a pasta foi baixada parcialmente.'
                raise DownloadError('Limite da pasta atingido.')
            if now - file_started[filename] > MAX_PIN_SECONDS:
                raise DownloadError('Pin excedeu o tempo máximo permitido.')

        job['message'] = 'Lendo a pasta e baixando...'
        with YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            # Também aceita pins de imagem e GIF em pastas.
            'format': 'bestvideo+bestaudio/best/bestimage/best',
            'merge_output_format': 'mp4',
            'outtmpl': str(media / '%(id)s.%(ext)s'),
            'noplaylist': False,
            'playlistend': MAX_BOARD_PINS,
            'ignoreerrors': True,
            'max_filesize': MAX_FILE_SIZE,
            'socket_timeout': 20,
            'retries': 2,
            'fragment_retries': 2,
            'progress_hooks': [hook],
        }) as ydl:
            ydl.download([board_url])

        files = sorted(
            f for f in media.iterdir()
            if f.is_file() and not f.name.endswith(('.part', '.ytdl'))
        )
        if not files:
            raise ValueError('Nenhum vídeo baixado')
        job['done'] = job['total'] = max(job['total'], len(files))
        job['skipped'] = max(0, job['total'] - len(files))
        job['message'] = 'Compactando arquivos...'
        archive = folder / f'pindown-{slug}.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_STORED) as zf:
            for f in files:
                zf.write(f, f.name)
        shutil.rmtree(media, ignore_errors=True)
        job['zip'] = archive
        job['filename'] = archive.name
        job['count'] = len(files)
        job['status'] = 'done'
    except Exception as exc:
        logger.warning('Board download failed (%s)', type(exc).__name__)
        job['status'] = 'error'
        job['message'] = 'Não foi possível baixar esta pasta. Confira se ela é pública e contém vídeos.'
        shutil.rmtree(media, ignore_errors=True)
    finally:
        job['finished'] = monotonic()
        board_slots.release()

@app.post('/api/board/info', dependencies=[Depends(require_password)])
def board_info(link: Link, request: Request):
    check_rate_limit(request, 'board_info', 6)
    url, _slug = validate_board(link.url)
    try:
        title, pins = list_board(url, MAX_BOARD_PINS)
    except Exception as exc:
        logger.warning('Board preview failed (%s)', type(exc).__name__)
        raise HTTPException(
            422,
            'Não foi possível ler a pasta. Verifique se ela é pública (não secreta) e tente novamente.',
        ) from exc
    return {
        'title': title,
        'count': min(len(pins), MAX_BOARD_PINS),
        'truncated': len(pins) > MAX_BOARD_PINS,
        'limit': MAX_BOARD_PINS,
    }

@app.post('/api/board/start', dependencies=[Depends(require_password)])
def board_start(link: Link, request: Request):
    check_rate_limit(request, 'board_start', 2)
    url, slug = validate_board(link.url)
    purge_jobs()
    if not board_slots.acquire(blocking=False):
        raise HTTPException(429, 'Já existe uma pasta sendo baixada. Tente novamente em instantes.')
    job_id = secrets.token_urlsafe(16)
    try:
        folder = Path(tempfile.mkdtemp(prefix='pindown_board_'))
        jobs[job_id] = {
            'status': 'running', 'total': 0, 'done': 0, 'skipped': 0, 'count': 0,
            'message': 'Lendo a pasta...', 'note': '', 'title': '',
            'folder': folder, 'zip': None, 'filename': None, 'finished': 0.0,
        }
        Thread(target=run_board_job, args=(job_id, url, slug), daemon=True).start()
    except Exception:
        jobs.pop(job_id, None)
        board_slots.release()
        raise
    return {'job_id': job_id}

@app.get('/api/board/{job_id}', dependencies=[Depends(require_password)])
def board_status(job_id: str, request: Request):
    check_rate_limit(request, 'board_status', 120)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'Download não encontrado ou expirado.')
    return {k: job[k] for k in ('status', 'total', 'done', 'skipped', 'count', 'message', 'note', 'title')}

@app.get('/api/board/{job_id}/file', dependencies=[Depends(require_password)])
def board_file(job_id: str, request: Request):
    check_rate_limit(request, 'board_file', 20)
    job = jobs.get(job_id)
    if not job or job['status'] != 'done' or not job['zip'] or not Path(job['zip']).exists():
        raise HTTPException(404, 'Arquivo não encontrado ou expirado.')
    return FileResponse(job['zip'], filename=job['filename'], media_type='application/zip')

@app.get('/manifest.webmanifest')
def manifest():
    return FileResponse(ROOT / 'manifest.webmanifest', media_type='application/manifest+json')

@app.get('/sw.js')
def service_worker():
    return FileResponse(ROOT / 'sw.js', media_type='application/javascript')

def cleanup_download(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError:
        logger.exception('Could not remove temporary download directory')
    finally:
        download_slots.release()
