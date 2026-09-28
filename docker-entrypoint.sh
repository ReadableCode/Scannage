#!/bin/sh
# Deliberately inert: schema convergence is the app's job (the store's
# bootstrap, run from the FastAPI lifespan), so bare local runs behave
# identically.
exec "$@"
