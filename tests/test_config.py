from copy import deepcopy
import pytest
from game.config import RULES, validate_rules


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("energy", "uranium_per_nuclear", 0),
        ("energy", "condition_max", 101),
        ("country", "initial_hunger", -1),
        ("market", "system_sell_multiplier", 1.5),
        ("market", "system_buy_multiplier", 0.5),
        ("jobs", "production_seconds", 0),
        ("recruitment", "maximum_share", float("nan")),
    ],
)
def test_invalid_economy_is_rejected(section, key, value):
    rules = deepcopy(RULES)
    rules[section][key] = value
    with pytest.raises(ValueError):
        validate_rules(rules)


def test_current_configuration_is_valid():
    validate_rules(RULES)
