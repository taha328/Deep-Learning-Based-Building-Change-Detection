.PHONY: postgres-setup postgres-health

postgres-setup:
	cd backend && python scripts/setup_postgis_db.py --migrate --verify

postgres-health:
	curl http://127.0.0.1:8000/api/health/db
