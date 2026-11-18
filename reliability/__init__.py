"""Fleet reliability analysis services.

Three independently runnable services share one SQLite database:

* ``ingestor``  -- consumes append-only fault / observation data
* ``builder``   -- turns canonical data into analysis samples + risk reports
* ``report``    -- serves the read API and web UI

Module boundaries are deliberately one-directional: the ingestor never
imports analysis code and the report service never writes canonical data.
"""

__version__ = "1.0.0"
