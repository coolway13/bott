"""Original untrained pregame heuristic. No network or Airtable dependency."""
import math
from datetime import datetime

def number(game, key, low=None, high=None):
    value = game.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{key} must be a finite number')
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f'{key} is outside the allowed range')
    return value


def calculate_probability(game):
    """Preserve the supplied heuristic; these weights have not been trained."""
    for side in ('away', 'home'):
        for metric in ('pitcher_era', 'pitcher_fip', 'bullpen_fip'):
            number(game, f'{side}_{metric}', 0)
        number(game, f'{side}_wrc_plus')
        number(game, f'{side}_xwoba', 0, 1)
        number(game, f'{side}_lineup_adjustment', -1, 1)
    score = (
        game['home_pitcher_era'] - game['away_pitcher_era']
        + (game['home_pitcher_fip'] - game['away_pitcher_fip']) * .7
        + (game['away_wrc_plus'] - game['home_wrc_plus']) / 25
        + (game['away_xwoba'] - game['home_xwoba']) * 15
        + (game['home_bullpen_fip'] - game['away_bullpen_fip']) * .5
        + (game['away_lineup_adjustment'] - game['home_lineup_adjustment']) * 20
        - .25
    )
    if not math.isfinite(score):
        raise ValueError('Model score overflowed; check inputs')
    # Stable logistic function avoids overflow for strongly negative scores.
    if score >= 0:
        return 1 / (1 + math.exp(-score))
    exp_score = math.exp(score)
    return exp_score / (1 + exp_score)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('start_time must be an ISO timestamp with timezone')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('start_time must include a timezone, such as -07:00 or Z')
    return dt


