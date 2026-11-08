"""The front door: always on, holds the queue and the channels, and starts a worker only while there is work
(docs/plans/scale-to-zero.md). See app.py for the HTTP APIs and core.py for what runs where."""
