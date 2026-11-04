# Newsletter architecture (public outline)

The newsletter is an independent scheduled digest. Its owner gathers dated public
reports, selects a bounded candidate set, uses an editorial pass to prepare a
short source-linked issue, validates references and claims against available
evidence, and publishes an archive page and a separate message delivery.

The local owner keeps immutable edition snapshots, editorial decisions and
delivery receipts so retries do not silently rewrite an already issued report.
Its static archive is generated output, not an editorial source of truth.
The newsletter has one narrow read-only bridge to enabled source metadata owned
by the reading-feed system; it does not consume that system's recommendations,
history, or user-interest signals. It does not write to the feed or the wiki,
tasks, or journal. The homepage supplies a route, not editorial state.

This public outline intentionally omits actual newsletters and editions, publisher
and source names or URLs, source configuration, extractors, editorial policy,
production code and tests, schedules, credentials, and delivery records. It is
not a deployable newsletter or a restore backup.
