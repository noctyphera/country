"""Validated configuration. No secrets belong in the balance files."""

from __future__ import annotations
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def load_rules(directory: Path | None = None) -> dict:
    result: dict = {}
    for path in sorted((directory or Path(os.getenv("GAME_CONFIG_DIR", ROOT / "config"))).glob("*.json")):
        data = json.loads(path.read_text())
        duplicates = result.keys() & data.keys()
        if duplicates:
            raise ValueError(f"Duplicate config sections in {path}: {duplicates}")
        result.update(data)
    if not result:
        raise ValueError("Game configuration directory is empty")
    validate_rules(result)
    return result


def validate_rules(rules: dict) -> None:
    """Reject invalid economic configurations before opening a database connection."""

    def require(condition: bool, message: str):
        if not condition:
            raise ValueError(message)

    def finite_tree(value, path="config"):
        if isinstance(value, dict):
            for key, child in value.items():
                finite_tree(child, f"{path}.{key}")
        elif isinstance(value, list):
            for child in value:
                finite_tree(child, path)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            require(math.isfinite(value) and value >= 0, f"Expected finite nonnegative number: {path}")

    finite_tree(rules)
    resources = rules["resources"]
    require({"food", "water", "uranium", "rocket"} <= resources.keys(), "Missing essential resources")
    for key, spec in resources.items():
        require(type(spec["price"]) is int and spec["price"] > 0, f"Invalid resource price: {key}")
    for category in ("buildings", "units"):
        for key, spec in rules[category].items():
            require(type(spec["money"]) is int and spec["money"] >= 0, f"Invalid cost: {key}")
            require(spec["cost"].keys() <= resources.keys(), f"Unknown cost resource: {key}")
            require(all(type(v) is int and v >= 0 for v in spec["cost"].values()), f"Invalid resource cost: {key}")
            if category == "buildings":
                require(spec["land"] > 0 and spec["output"] >= 0, f"Invalid building: {key}")
                require(not spec.get("resource") or spec["resource"] in resources, f"Unknown output: {key}")
            else:
                require(spec["power"] > 0, f"Invalid military power: {key}")
    for key, value in rules["jobs"].items():
        require(value > 0, f"Invalid job interval or retention: {key}")
    require(rules["energy"]["uranium_per_nuclear"] > 0, "Nuclear fuel consumption must be positive")
    require(0 < rules["energy"]["condition_max"] <= 100, "Energy condition must be within 0..100")
    for key in ("initial_hunger", "initial_army_hunger", "initial_energy_condition"):
        require(0 <= rules["country"][key] <= 100, f"Invalid initial condition: {key}")
    require(0 < rules["recruitment"]["maximum_share"] < 1, "Invalid recruitment share")
    require(0 <= rules["disasters"]["min_damage"] <= rules["disasters"]["max_damage"] < 1, "Invalid disaster damage")
    require(
        set(rules["disasters"]["kinds"]) <= {"virus", "meteor", "aliens", "fire", "earthquake"}
        and bool(rules["disasters"]["kinds"]),
        "Invalid disaster types",
    )
    require(0 <= rules["space"]["explosion_chance"] <= 1, "Invalid rocket explosion probability")
    require(rules["space"]["duration_hours"] > 0, "Invalid expedition duration")
    for key, bounds in rules["space"]["rewards"].items():
        require(key in resources and len(bounds) == 2 and 0 <= bounds[0] <= bounds[1], f"Invalid space reward: {key}")
    market = rules["market"]
    require(
        market["system_buy_multiplier"] >= 1 and 0 < market["system_sell_multiplier"] <= 1,
        "NPC prices must not allow arbitrage",
    )
    require(
        0 < market["transfer_max_balance_share"] <= 1 and 0 <= market["transfer_fee_share"] < 1,
        "Invalid transfer constraints",
    )
    require(
        rules["economy"]["house_capacity"] > 0 and rules["economy"]["income_population_floor"] > 0,
        "Invalid population denominators",
    )
    require(rules["resources"].keys().isdisjoint(rules["units"]), "Resource and unit keys must be distinct")
    require(rules["market"]["unit_prices"].keys() == rules["units"].keys(), "Every unit needs a market price")
    require(all(type(v) is int and v > 0 for v in rules["market"]["unit_prices"].values()), "Invalid unit price")
    require(
        0 < market["unit_export_multiplier"] <= market["unit_import_multiplier"], "Unit trade must not allow arbitrage"
    )
    war = rules["country_war"]
    require(0 <= war["power_tolerance"] < 1 and 0 <= war["strength_advantage"] <= 0.5, "Invalid war matching or odds")
    require(
        0 <= war["loot_share"] <= 1 and 0 <= war["loss_share"] <= 1 and war["duration_hours"] > 0,
        "Invalid country war costs or duration",
    )
    require(
        0 <= rules["alliance"]["loser_retention"] <= 1 and rules["alliance"]["reward_multiplier"] > 0,
        "Invalid alliance war rewards",
    )
    require(
        rules["economy"]["food_per_percent_army"] > 0 and rules["economy"]["food_per_percent_civilian"] > 0,
        "Invalid food allocation rate",
    )
    initial = rules["initial"]
    require(initial["resources"].keys() <= resources.keys(), "Unknown starting resource")
    require(initial["buildings"].keys() <= rules["buildings"].keys(), "Unknown starting building")
    used = sum(rules["buildings"][key]["land"] * value for key, value in initial["buildings"].items())
    require(used <= initial["land"], "Starting buildings exceed available land")


RULES = load_rules()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    poll_seconds: float = 5.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            database_url=os.getenv("DATABASE_URL", "postgresql+asyncpg://country:country@localhost:5432/country"),
            poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", RULES["jobs"]["poll_seconds"])),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
