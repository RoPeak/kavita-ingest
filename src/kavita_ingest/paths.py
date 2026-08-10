from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    config_file: Path
    database_file: Path
    log_file: Path

    @classmethod
    def default(cls) -> AppPaths:
        dirs = PlatformDirs("kavita-ingest", appauthor=False, ensure_exists=False)
        config_dir = Path(dirs.user_config_dir)
        data_dir = Path(dirs.user_data_dir)
        state_dir = Path(dirs.user_state_dir)
        cache_dir = Path(dirs.user_cache_dir)
        return cls(
            config_dir=config_dir,
            data_dir=data_dir,
            state_dir=state_dir,
            cache_dir=cache_dir,
            config_file=config_dir / "config.toml",
            database_file=state_dir / "state.sqlite3",
            log_file=state_dir / "kavita-ingest.log",
        )
