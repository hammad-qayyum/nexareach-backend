# pg_dump from the same Postgres major version as the database, plus restic for encrypted backups.
FROM postgres:16
RUN apt-get update \
    && apt-get install -y --no-install-recommends restic ca-certificates \
    && apt-get clean
COPY backup.sh restore-test.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore-test.sh
CMD ["backup.sh", "loop"]
