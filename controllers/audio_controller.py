"""
Controlador de áudio — NetWatch Pro v2.5.

ALTERAÇÕES v2.5:
  • delete_host_alerts(): para + unload pygame antes de apagar, com retry
    e fallback rename para WinError 32 (arquivo em uso pelo processo).
  • _play_pygame(): copia arquivos de caminhos UNC (rede SMB) para pasta
    temp local antes de carregar no pygame — resolve áudio silencioso no
    visualizador que acessa os MP3 via \\servidor\share\audio\.
  • resume() chamado explicitamente ao entrar no viewer loop.

v2.4 (mantidas):
  • Propriedade `muted` — silencia todos os alertas.
  • Preferência de mute persistida no user_prefs.json do usuário.
  • `generate_all_missing_alerts(hosts)` para startup.
  • Lock de reprodução para evitar sobreposição de áudio.

• Reprodução: pygame (MP3 + WAV)
• Geração automática: edge-tts voz FranciscaNeural (pt-BR)
• Cada alerta reproduz a voz DA LOJA duas vezes
• Sirene (Alerta.mp3) toca 4 s antes de cada aviso offline

Instalação das dependências:
    pip install edge-tts pygame
"""
import asyncio
import os
import platform
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from config import AUDIO_DIR, is_audio_muted, set_audio_muted
from utils.logger import setup_logger

logger = setup_logger("audio")

# ── Configuração TTS ──────────────────────────────────────────────────
TTS_VOICE        = "pt-BR-FranciscaNeural"
ALERTA_DURATION_S = 3
VOICE_REPEAT      = 2
VOICE_REPEAT_PAUSE_S = 0.6

# Pasta temp local para cópias de reprodução em caminhos de rede
_TEMP_AUDIO_DIR: Optional[Path] = None


def _get_local_temp_dir() -> Path:
    """Retorna (e cria) pasta temp local para cópias de áudio de rede."""
    global _TEMP_AUDIO_DIR
    if _TEMP_AUDIO_DIR is None:
        _TEMP_AUDIO_DIR = Path(tempfile.gettempdir()) / "netwatch_audio_cache"
        _TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return _TEMP_AUDIO_DIR


def _is_network_path(filepath: str) -> bool:
    """Retorna True se o caminho é UNC (\\servidor\share) ou mapeado."""
    p = filepath.replace("/", "\\")
    if p.startswith("\\\\"):
        return True
    # Drive letter mapeado para rede também pode ser lento — mas deixamos
    # para o pygame tentar; só copiamos caminhos UNC explícitos.
    return False


def _local_copy_for_playback(filepath: str) -> str:
    """
    Se o arquivo estiver em um caminho de rede UNC, copia para temp local
    e retorna o caminho local. Caso contrário, retorna o original.

    pygame no Windows não consegue tocar arquivos de caminhos UNC
    (\\servidor\share\arquivo.mp3) — o music.load() falha silenciosamente
    ou lança OSError. Copiar para temp local resolve o problema.
    """
    if not _is_network_path(filepath):
        return filepath
    try:
        src = Path(filepath)
        dst = _get_local_temp_dir() / src.name
        # Só copia se arquivo local não existe ou está desatualizado
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(str(src), str(dst))
        return str(dst)
    except Exception as e:
        logger.debug(f"Não foi possível copiar para temp local ({e}), usando original")
        return filepath


class AudioController:
    """Gerencia reprodução e geração de áudios de alerta."""

    def __init__(self):
        self._lock      = threading.Lock()
        self._play_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._muted     = is_audio_muted()
        self._pygame_ok = False
        self._init_pygame()
        logger.info(
            f"AudioController inicializado — pasta: {AUDIO_DIR} — "
            f"muted: {self._muted}"
        )

    # ── Inicialização ─────────────────────────────────────────────────

    def _init_pygame(self):
        try:
            import pygame
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self._pygame_ok = True
            logger.info("pygame.mixer OK (MP3 + WAV)")
        except ImportError:
            logger.warning("pygame não instalado — instale com: pip install pygame")
        except Exception as e:
            logger.warning(f"pygame falhou ao inicializar: {e}")

    # ── Propriedade muted ─────────────────────────────────────────────

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool):
        self._muted = value
        set_audio_muted(value)
        logger.info(f"Áudio {'silenciado' if value else 'ativado'}")
        if value:
            self.stop_all()
        else:
            # CORREÇÃO v2.6 — race condition no unmute:
            #
            #   stop_all() (chamado durante o mute) seta _stop_flag.
            #   A versão anterior limpava _stop_flag DENTRO de uma thread
            #   background (_safe_resume). Isso criava uma janela de tempo em
            #   que _muted já era False mas _stop_flag ainda estava setado:
            #   se um alerta chegasse nessa janela, play_alert() checava
            #   _stop_flag.is_set() == True e abortava silenciosamente.
            #
            #   Correção: limpa _stop_flag IMEDIATAMENTE (síncrono) aqui no
            #   setter, antes de qualquer thread nova poder ser disparada.
            #   Só a reinicialização do mixer (operação lenta, ~200ms) vai
            #   para background — ela não afeta novos play_alert(), que usam
            #   a API do mixer diretamente.
            self._stop_flag.clear()   # síncrono — sem janela de race condition

            def _reinit_in_bg():
                # Aguarda thread anterior (se alguma) liberar o lock (max 2s)
                acquired = self._play_lock.acquire(timeout=2.0)
                if acquired:
                    self._play_lock.release()
                # Reinicializa o mixer para estado limpo após o stop_all()
                self._reinit_pygame_mixer()

            threading.Thread(target=_reinit_in_bg, daemon=True,
                             name="audio-resume").start()

    def toggle_mute(self) -> bool:
        self.muted = not self._muted
        return self._muted

    # ── API pública ───────────────────────────────────────────────────

    def play_alert(self, label: str, alert_type: str = "offline"):
        """Toca a voz da loja VOICE_REPEAT vezes."""
        if self._muted:
            return

        safe_name  = _safe_filename(label)
        audio_file = _find_audio(safe_name, alert_type)

        def _play():
            if self._stop_flag.is_set() or self._muted:
                return
            # CORREÇÃO v2.8 — lock com timeout:
            #   O _play_lock anterior usava `with self._play_lock:` que
            #   bloqueia indefinidamente. Se _play_pygame travasse (pygame
            #   get_busy() retorna True para sempre — bug no Windows após
            #   muitas reproduções MP3), o lock nunca era liberado e TODAS
            #   as threads de áudio subsequentes ficavam bloqueadas para
            #   sempre, matando o sistema de alertas sonoros.
            #   Com timeout de 30s, a thread desiste e loga o problema
            #   em vez de bloquear permanentemente.
            acquired = self._play_lock.acquire(timeout=30)
            if not acquired:
                logger.warning(
                    f"play_alert({label}, {alert_type}): _play_lock ocupado "
                    "há 30s — pulando áudio (possível travamento no pygame)"
                )
                return
            try:
                if audio_file:
                    for i in range(VOICE_REPEAT):
                        if self._stop_flag.is_set() or self._muted:
                            break
                        self._play_file_sync(str(audio_file))
                        if i < VOICE_REPEAT - 1:
                            time.sleep(VOICE_REPEAT_PAUSE_S)
                else:
                    logger.warning(
                        f"Áudio não encontrado: {safe_name}_{alert_type}.[mp3|wav]"
                    )
            finally:
                self._play_lock.release()

        threading.Thread(target=_play, daemon=True).start()

    def play_generic_alert(self):
        """Toca Alerta.mp3 por no máximo ALERTA_DURATION_S segundos."""
        if self._muted:
            return

        def _play():
            if self._stop_flag.is_set() or self._muted:
                return
            # CORREÇÃO v2.8 — lock com timeout (mesmo motivo acima)
            acquired = self._play_lock.acquire(timeout=30)
            if not acquired:
                logger.warning(
                    "play_generic_alert: _play_lock ocupado há 30s — "
                    "pulando áudio (possível travamento no pygame)"
                )
                return
            try:
                alerta = _find_alerta()
                if alerta:
                    self._play_file_timed(str(alerta), ALERTA_DURATION_S)
                else:
                    logger.warning(
                        f"Alerta.mp3 não encontrado em {AUDIO_DIR}. "
                        "Coloque o arquivo como Alerta.mp3 nessa pasta."
                    )
                    self._beep_fallback()
            finally:
                self._play_lock.release()

        threading.Thread(target=_play, daemon=True).start()

    def generate_host_alerts(self, label: str):
        """Gera {label}_offline.mp3 e {label}_online.mp3 via edge-tts."""
        def _gen():
            safe = _safe_filename(label)
            pairs = [
                (f"{label}, está off-line.", AUDIO_DIR / f"{safe}_offline.mp3"),
                (f"{label}, está on-line.",  AUDIO_DIR / f"{safe}_online.mp3"),
            ]
            for text, path in pairs:
                if path.exists():
                    continue
                ok = _edge_tts_generate_sync(text, TTS_VOICE, path)
                if ok:
                    logger.info(f"Áudio gerado: {path.name}")
                else:
                    logger.warning(f"Falha ao gerar áudio: {path.name}")

        threading.Thread(target=_gen, daemon=True).start()

    def generate_all_missing_alerts(self, hosts: list):
        """Gera áudios TTS para todos os hosts sem arquivo (chamado no startup)."""
        def _gen_all():
            generated = 0
            for host in hosts:
                label = host.display_name if hasattr(host, "display_name") else str(host)
                if not label:
                    continue
                safe = _safe_filename(label)
                pairs = [
                    (f"{label}, está off-line.", AUDIO_DIR / f"{safe}_offline.mp3"),
                    (f"{label}, está on-line.",  AUDIO_DIR / f"{safe}_online.mp3"),
                ]
                for text, path in pairs:
                    if path.exists():
                        continue
                    ok = _edge_tts_generate_sync(text, TTS_VOICE, path)
                    if ok:
                        generated += 1
            if generated:
                logger.info(f"Áudios gerados no startup: {generated} arquivos")

        threading.Thread(target=_gen_all, daemon=True, name="audio-gen-all").start()

    def delete_host_alerts(self, label: str):
        """
        Remove arquivos de áudio de um host ao excluí-lo.

        CORREÇÃO v2.10 — força remoção matando o processo de áudio:
          A versão anterior tentava "educadamente" por 120s e desistia.
          Agora: reinicializa o mixer inteiro (quit + init) para liberar
          TODOS os handles de arquivo, depois deleta. Se ainda falhar,
          usa rename para .del e limpa depois.
        """
        safe = _safe_filename(label)

        # Passo 1: MATA o pygame mixer completamente — libera todos os handles
        self._force_release_pygame()

        # Limpa resíduos .del de tentativas anteriores
        for p_del in AUDIO_DIR.glob(f"{safe}_*.del"):
            try:
                p_del.unlink()
            except Exception:
                pass

        deleted = []
        failed  = []

        for suffix in ("offline", "online"):
            for ext in ("mp3", "wav"):
                p = AUDIO_DIR / f"{safe}_{suffix}.{ext}"
                if not p.exists():
                    continue
                if self._try_delete(p):
                    deleted.append(p.name)
                else:
                    failed.append(p)

        # Limpa também as cópias no cache temp local (viewer via rede SMB)
        self._delete_local_cache(safe)

        # Passo 3: se ainda falhou, tenta UMA vez mais com kill total
        if failed:
            self._force_release_pygame()
            time.sleep(0.3)
            still_failed = []
            for p in failed:
                if p.exists():
                    if self._try_delete(p):
                        deleted.append(p.name)
                    else:
                        still_failed.append(p)
            failed = still_failed

        # Passo 4: último recurso — rename para .del
        if failed:
            for p in failed:
                try:
                    tmp = p.with_suffix(".del")
                    p.rename(tmp)
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    deleted.append(p.name)
                except Exception:
                    pass

        if deleted:
            logger.info(f"Áudios removidos para '{label}': {', '.join(deleted)}")

        # Reinicializa o mixer para uso futuro
        self._reinit_pygame_mixer()

    def _delete_local_cache(self, safe_name: str):
        """
        Remove as cópias de reprodução do cache temp local.
        Essas cópias são criadas por _local_copy_for_playback() quando
        o viewer toca MP3s de um caminho UNC de rede.
        """
        try:
            cache_dir = _get_local_temp_dir()
            for suffix in ("offline", "online"):
                for ext in ("mp3", "wav"):
                    cached = cache_dir / f"{safe_name}_{suffix}.{ext}"
                    if cached.exists():
                        try:
                            cached.unlink()
                        except Exception:
                            pass
        except Exception:
            pass

    def _pygame_stop_and_unload(self):
        """Para a reprodução e libera o file handle do pygame."""
        if not self._pygame_ok:
            return
        try:
            import pygame
            pygame.mixer.music.stop()
            # unload() disponível no pygame >= 2.0; libera o handle do arquivo
            if hasattr(pygame.mixer.music, "unload"):
                pygame.mixer.music.unload()
            else:
                # Fallback: carrega um buffer vazio para liberar o arquivo anterior
                import io
                pygame.mixer.music.load(io.BytesIO(b""))
        except Exception:
            pass

    def _force_release_pygame(self):
        """
        CORREÇÃO v2.10 — mata o pygame mixer completamente para liberar
        TODOS os file handles. Mais agressivo que _pygame_stop_and_unload().

        Necessário quando se quer deletar um áudio que está em uso.
        O mixer é reinicializado depois via _reinit_pygame_mixer().
        """
        if not self._pygame_ok:
            return
        try:
            import pygame
            pygame.mixer.music.stop()
            if hasattr(pygame.mixer.music, "unload"):
                pygame.mixer.music.unload()
            pygame.mixer.quit()
            time.sleep(0.1)
            logger.debug("pygame.mixer encerrado para liberação de handles")
        except Exception:
            pass

    def _try_delete(self, p: Path, attempts: int = 3, delay: float = 0.3) -> bool:
        """
        Tenta apagar o arquivo com retry. Se ainda bloqueado no Windows,
        tenta renomear para .del (libera o nome original) e então apaga.
        Retorna True se conseguiu apagar de alguma forma.
        """
        for i in range(attempts):
            try:
                p.unlink()
                return True
            except PermissionError:
                if i < attempts - 1:
                    time.sleep(delay)
                else:
                    # Última tentativa: rename para .del e apaga
                    try:
                        tmp = p.with_suffix(".del")
                        p.rename(tmp)
                        try:
                            tmp.unlink()
                        except Exception:
                            pass  # .del será limpo na próxima vez
                        return True
                    except Exception:
                        return False
            except Exception as e:
                logger.warning(f"Erro ao apagar {p.name}: {e}")
                return False
        return False

    def _delete_when_free(self, paths: list, timeout_s: float = 120.0):
        """
        Tenta apagar arquivos que estavam bloqueados em background.
        Aguarda até timeout_s (padrão 120s) — cobre casos de TTS lento
        (edge-tts em rede lenta) ou outro processo segurando o handle.
        Intervalo de tentativa: 0.5s para reagir rápido quando liberar.
        """
        deadline = time.time() + timeout_s
        remaining = list(paths)
        while remaining and time.time() < deadline:
            time.sleep(0.5)
            # CORREÇÃO v2.6 — _delete_when_free interrompia áudio em andamento:
            #   A chamada incondicional a _pygame_stop_and_unload() a cada 0.5s
            #   cortava qualquer alerta sonoro em reprodução (sirene de 7s, voz).
            #   Correção: só para/descarrega o mixer se NADA estiver tocando,
            #   verificado via _play_lock.acquire(blocking=False).
            can_stop = self._play_lock.acquire(blocking=False)
            if can_stop:
                self._play_lock.release()
                self._pygame_stop_and_unload()
            still_locked = []
            for p in remaining:
                # Também tenta limpar .del residual
                p_del = p.with_suffix(".del")
                if p_del.exists():
                    try:
                        p_del.unlink()
                    except Exception:
                        pass
                if not p.exists():
                    continue
                if self._try_delete(p, attempts=1):
                    logger.info(f"Áudio removido (deferred): {p.name}")
                else:
                    still_locked.append(p)
            remaining = still_locked

        if remaining:
            for p in remaining:
                logger.warning(
                    f"Não foi possível apagar {p.name} após {timeout_s:.0f}s "
                    "(arquivo em uso por outro processo)"
                )

    def stop_all(self):
        self._stop_flag.set()
        if self._pygame_ok:
            try:
                import pygame
                pygame.mixer.music.stop()
                if hasattr(pygame.mixer.music, "unload"):
                    pygame.mixer.music.unload()
            except Exception:
                pass

    def shutdown(self):
        """
        CORREÇÃO v2.8 — encerramento completo do subsistema de áudio.
        Chamado durante o fechamento da aplicação para liberar o driver
        de áudio do Windows e permitir que o processo termine.
        """
        self.stop_all()
        if self._pygame_ok:
            try:
                import pygame
                pygame.mixer.quit()
                self._pygame_ok = False
                logger.debug("pygame.mixer encerrado")
            except Exception:
                pass

    def resume(self):
        self._stop_flag.clear()

    def _reinit_pygame_mixer(self):
        """
        Reinicializa o pygame mixer.

        CORREÇÃO v2.6: após ciclos de mute/unmute (stop_all + resume),
        o pygame mixer no Windows pode ficar em estado inconsistente —
        music.play() é chamado mas nenhum som sai. Reinicializar o mixer
        (quit + init) resolve o problema de forma confiável.
        """
        if not self._pygame_ok:
            return
        try:
            import pygame
            pygame.mixer.music.stop()
            if hasattr(pygame.mixer.music, "unload"):
                pygame.mixer.music.unload()
            pygame.mixer.quit()
            time.sleep(0.1)
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            logger.debug("pygame.mixer reinicializado após unmute")
        except Exception as e:
            logger.warning(f"Falha ao reinicializar pygame mixer: {e}")
            # Tenta init mesmo que quit tenha falhado
            try:
                import pygame
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            except Exception:
                pass

    # ── Reprodução interna ────────────────────────────────────────────

    def _play_file_sync(self, filepath: str):
        """Reproduz arquivo completamente (bloqueante)."""
        if self._stop_flag.is_set() or self._muted:
            return
        # Copia para temp local se for caminho UNC de rede
        local = _local_copy_for_playback(filepath)
        if self._pygame_ok:
            self._play_pygame(local, max_seconds=None)
        else:
            self._play_system(filepath)  # system player lida bem com UNC

    def _play_file_timed(self, filepath: str, max_seconds: float):
        """Reproduz por no máximo max_seconds segundos."""
        if self._stop_flag.is_set() or self._muted:
            return
        local = _local_copy_for_playback(filepath)
        if self._pygame_ok:
            self._play_pygame(local, max_seconds=max_seconds)
        else:
            self._play_system(filepath)

    def _play_pygame(self, filepath: str, max_seconds: Optional[float]):
        # CORREÇÃO v2.8 — timeout absoluto:
        #   Se o pygame travar no get_busy() (driver de áudio congelado, codec
        #   corrompido), o loop while nunca terminava, travando o _play_lock
        #   indefinidamente. Isso impedia qualquer novo áudio de tocar e
        #   eventualmente acumulava threads de áudio bloqueadas.
        #   Agora há um timeout absoluto de max_seconds ou 30s (o que for menor).
        absolute_max = min(max_seconds or 30, 30)
        try:
            import pygame
            pygame.mixer.music.load(filepath)
            pygame.mixer.music.play()

            start = time.time()
            while pygame.mixer.music.get_busy():
                if self._stop_flag.is_set() or self._muted:
                    break
                if (time.time() - start) >= absolute_max:
                    logger.debug(f"Timeout absoluto de {absolute_max}s atingido para '{filepath}'")
                    break
                time.sleep(0.05)
            pygame.mixer.music.stop()

        except Exception as e:
            logger.error(f"pygame erro '{filepath}': {e}")
            # Tenta reinicializar o mixer após erro — pode estar em estado corrupto
            try:
                import pygame
                pygame.mixer.music.stop()
                if hasattr(pygame.mixer.music, "unload"):
                    pygame.mixer.music.unload()
            except Exception:
                pass
            self._play_system(filepath)

    def _play_system(self, filepath: str):
        """Fallback sem pygame."""
        if platform.system() == "Windows":
            if filepath.lower().endswith(".wav"):
                try:
                    import winsound
                    winsound.PlaySound(filepath, winsound.SND_FILENAME)
                    return
                except Exception:
                    pass
            try:
                os.startfile(filepath)
            except Exception:
                pass
        else:
            os.system(
                f'mpg123 -q "{filepath}" 2>/dev/null || '
                f'ffplay -nodisp -autoexit -loglevel quiet "{filepath}" 2>/dev/null || '
                f'aplay "{filepath}" 2>/dev/null'
            )

    def _beep_fallback(self):
        if self._muted:
            return
        if platform.system() == "Windows":
            try:
                import winsound
                for f, d in [(880, 350), (660, 250), (880, 350)]:
                    winsound.Beep(f, d)
                    time.sleep(0.1)
            except Exception:
                pass


# ── Utilitários ───────────────────────────────────────────────────────

def _safe_filename(label: str) -> str:
    # CORREÇÃO v2.12 — usa safe_filename do módulo security (whitelist [A-Za-z0-9_-])
    from utils.security import safe_filename
    return safe_filename(label, max_len=80)


def _find_audio(safe_name: str, alert_type: str) -> Optional[Path]:
    for ext in ("mp3", "wav"):
        p = AUDIO_DIR / f"{safe_name}_{alert_type}.{ext}"
        if p.exists():
            return p
    return None


def _find_alerta() -> Optional[Path]:
    candidates = [
        AUDIO_DIR / "Alerta.mp3",
        AUDIO_DIR / "alerta.mp3",
        Path(r"C:\Alerta\Alerta.mp3"),
        AUDIO_DIR / "alerta_generico.wav",
    ]
    return next((f for f in candidates if f.exists()), None)


def _edge_tts_generate_sync(text: str, voice: str, filepath: Path) -> bool:
    try:
        import edge_tts

        async def _gen():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(filepath))

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, _gen())
                    future.result(timeout=30)
            else:
                loop.run_until_complete(_gen())
        except RuntimeError:
            asyncio.run(_gen())

        return filepath.exists()

    except ImportError:
        logger.warning("edge-tts não instalado — instale com: pip install edge-tts")
        return False
    except Exception as e:
        logger.error(f"edge-tts falhou para '{text}': {e}")
        return False
