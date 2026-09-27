"""Golden-day scenario harness — a whole day described as data, run through
the REAL reconstruction (`analyst.blocks`) and the REAL ping composer
(`pings.compose`), checked against readable expectations about the result.

See `scenarios.py` for the harness itself and `scenario_data.py` for the
scenarios. Every scenario is drawn from a real defect (HANDOFF.md), so a
regression is a scenario turning red, not a diff someone has to notice.
"""
