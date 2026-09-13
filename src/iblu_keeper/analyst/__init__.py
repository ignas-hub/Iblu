"""The analyst — turning signals into a day (plan §12).

Collectors record *what happened*. The analyst answers *where the day went*:
it clusters signals into `blocks`, judges each block's attention against the
intent calendar, and mirrors the result onto the Secretary calendar so Ignas
can see the day he actually had next to the day he planned.

Three rules from the mission constrain everything in here:

  * **Silence is never presence.** A stretch with no signals never becomes
    work. It becomes an `ambiguous` block only where an intent existed and
    produced nothing — and where there was no intent either, it produces no
    block at all, because inventing one would be inventing a fact.
  * **Attention, not location.** A block says where his attention was, not
    where his body was. A three-hour "football" intent can come back as
    30 min present / 90 min Deadlift / 60 min ambiguous.
  * **A correction supersedes, it never deletes.** Rebuilding a day marks the
    previous blocks `superseded_by` and keeps them.
"""

from .blocks import reconstruct  # noqa: F401
