-- nba-stats schema
--
-- Design principle: player_games is the grain of the whole database. One row
-- per player per game. Every season aggregate, per-mode conversion and derived
-- "random stat" (double-doubles, single-digit scoring seasons, 40-point games)
-- is computed from this table at query time. Nothing is pre-aggregated and
-- stored, because pre-aggregation is what forces a rewrite the first time
-- someone asks a question you didn't anticipate.
--
-- Completed seasons are immutable, so this database is write-once per season
-- and read-many forever.

PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------------
-- Dimension tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS players (
    player_id     INTEGER PRIMARY KEY,
    full_name     TEXT    NOT NULL,
    first_name    TEXT,
    last_name     TEXT,
    is_active     INTEGER          -- 1/0, from nba_api static data
);

CREATE TABLE IF NOT EXISTS teams (
    team_id       INTEGER PRIMARY KEY,
    abbreviation  TEXT    NOT NULL,
    full_name     TEXT    NOT NULL,
    nickname      TEXT,
    city          TEXT,
    state         TEXT,
    year_founded  INTEGER
);


-- ---------------------------------------------------------------------------
-- Games
--
-- NBA game logs arrive one row per TEAM per game, so a single game appears
-- twice. We collapse that into one row here with an explicit home/away side.
-- MATCHUP strings use "vs." for home and "@" for away, which is how the
-- parser decides.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS games (
    game_id         TEXT PRIMARY KEY,
    season          INTEGER NOT NULL,   -- 2015 means the 2015-16 season
    season_type     TEXT    NOT NULL,   -- 'Regular Season' | 'Playoffs' | 'Pre Season'
    game_date       TEXT    NOT NULL,   -- ISO: YYYY-MM-DD
    home_team_id    INTEGER REFERENCES teams(team_id),
    away_team_id    INTEGER REFERENCES teams(team_id),
    home_pts        INTEGER,
    away_pts        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_games_season      ON games(season, season_type);
CREATE INDEX IF NOT EXISTS idx_games_date        ON games(game_date);


-- ---------------------------------------------------------------------------
-- player_games: the core table
--
-- Storing counting stats only. Percentages (FG_PCT etc.) are deliberately NOT
-- stored: they are derived (fgm/fga) and storing them invites the classic bug
-- of averaging percentages across games instead of recomputing from totals.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS player_games (
    game_id     TEXT    NOT NULL REFERENCES games(game_id),
    player_id   INTEGER NOT NULL REFERENCES players(player_id),
    team_id     INTEGER NOT NULL REFERENCES teams(team_id),
    season      INTEGER NOT NULL,
    season_type TEXT    NOT NULL,
    game_date   TEXT    NOT NULL,
    wl          TEXT,               -- 'W' | 'L'
    min         REAL,               -- minutes played
    fgm         INTEGER,
    fga         INTEGER,
    fg3m        INTEGER,
    fg3a        INTEGER,
    ftm         INTEGER,
    fta         INTEGER,
    oreb        INTEGER,
    dreb        INTEGER,
    reb         INTEGER,
    ast         INTEGER,
    stl         INTEGER,
    blk         INTEGER,
    tov         INTEGER,
    pf          INTEGER,
    pts         INTEGER,
    plus_minus  INTEGER,
    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_pg_player_season ON player_games(player_id, season, season_type);
CREATE INDEX IF NOT EXISTS idx_pg_season        ON player_games(season, season_type);
CREATE INDEX IF NOT EXISTS idx_pg_team_season   ON player_games(team_id, season, season_type);
CREATE INDEX IF NOT EXISTS idx_pg_date          ON player_games(game_date);


-- ---------------------------------------------------------------------------
-- team_games: one row per team per game
--
-- Possessions are estimated, not official. The standard approximation is
-- stored so ORtg/DRtg and per-100 conversions are consistent everywhere.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS team_games (
    game_id      TEXT    NOT NULL REFERENCES games(game_id),
    team_id      INTEGER NOT NULL REFERENCES teams(team_id),
    opp_team_id  INTEGER REFERENCES teams(team_id),
    season       INTEGER NOT NULL,
    season_type  TEXT    NOT NULL,
    game_date    TEXT    NOT NULL,
    is_home      INTEGER,            -- 1/0
    wl           TEXT,
    min          REAL,
    fgm          INTEGER,
    fga          INTEGER,
    fg3m         INTEGER,
    fg3a         INTEGER,
    ftm          INTEGER,
    fta          INTEGER,
    oreb         INTEGER,
    dreb         INTEGER,
    reb          INTEGER,
    ast          INTEGER,
    stl          INTEGER,
    blk          INTEGER,
    tov          INTEGER,
    pf           INTEGER,
    pts          INTEGER,
    plus_minus   INTEGER,
    PRIMARY KEY (game_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_tg_team_season ON team_games(team_id, season, season_type);
CREATE INDEX IF NOT EXISTS idx_tg_season      ON team_games(season, season_type);


-- ---------------------------------------------------------------------------
-- Ingest bookkeeping
--
-- This is what makes the backfill resumable and idempotent. Before pulling a
-- season we check for a 'complete' row here and skip it. A crash mid-backfill
-- costs you one season, not thirty.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_log (
    season       INTEGER NOT NULL,
    season_type  TEXT    NOT NULL,
    status       TEXT    NOT NULL,   -- 'pending' | 'complete' | 'failed'
    rows_player  INTEGER,
    rows_team    INTEGER,
    error        TEXT,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (season, season_type)
);


-- ---------------------------------------------------------------------------
-- Convenience views
-- ---------------------------------------------------------------------------

-- Season totals per player per team-season. A traded player has one row per
-- team plus the caller can SUM across them for a combined season line.
CREATE VIEW IF NOT EXISTS player_season_totals AS
SELECT
    pg.player_id,
    p.full_name AS player_name,
    pg.season,
    pg.season_type,
    COUNT(*)            AS gp,
    SUM(pg.min)         AS min,
    SUM(pg.fgm)         AS fgm,
    SUM(pg.fga)         AS fga,
    SUM(pg.fg3m)        AS fg3m,
    SUM(pg.fg3a)        AS fg3a,
    SUM(pg.ftm)         AS ftm,
    SUM(pg.fta)         AS fta,
    SUM(pg.oreb)        AS oreb,
    SUM(pg.dreb)        AS dreb,
    SUM(pg.reb)         AS reb,
    SUM(pg.ast)         AS ast,
    SUM(pg.stl)         AS stl,
    SUM(pg.blk)         AS blk,
    SUM(pg.tov)         AS tov,
    SUM(pg.pf)          AS pf,
    SUM(pg.pts)         AS pts,
    SUM(pg.plus_minus)  AS plus_minus
FROM player_games pg
JOIN players p ON p.player_id = pg.player_id
GROUP BY pg.player_id, pg.season, pg.season_type;
