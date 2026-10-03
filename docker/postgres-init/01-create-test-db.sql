-- Runs once, when the db volume is first initialised. pytest uses docs_test and never
-- touches docs; tests/conftest.py also creates docs_test if this script did not run
-- (a volume created before the script existed).
create database docs_test;
