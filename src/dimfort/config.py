"""Project config loading (e.g. .dimfort.toml). Stub."""
from dataclasses import dataclass, field


@dataclass
class Config:
    include: list[str] = field(default_factory=lambda: ["**/*.f90", "**/*.F90"])
    exclude: list[str] = field(default_factory=list)


def load(path: str | None = None) -> Config:
    return Config()
