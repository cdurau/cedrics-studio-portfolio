#!/bin/sh
set -eu
node dist/server/server/bootstrap.js
unset MIGRATION_DATABASE_URL ADMIN_EMAIL ADMIN_PASSWORD
exec node dist/server/server/server.js
