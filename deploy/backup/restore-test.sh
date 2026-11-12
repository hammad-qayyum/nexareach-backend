#!/bin/sh
# Monthly restore test (plan section 22): restore the latest backup into a scratch database, check it,
# then drop it. The live database is never touched.
set -eu
scratch="restore_test_$(date -u +%Y%m%d%H%M)"
echo "restoring the latest backup into $scratch"
createdb "$scratch"
restic dump --tag db latest nexareach.dump | pg_restore --no-owner --dbname "$scratch"
echo "row counts in the restored copy:"
psql --dbname "$scratch" -Atc "
  select 'users', count(*) from users union all
  select 'leads', count(*) from leads union all
  select 'campaigns', count(*) from campaigns union all
  select 'campaign_recipients', count(*) from campaign_recipients union all
  select 'activity_logs', count(*) from activity_logs"
dropdb "$scratch"
echo "restore test passed"
