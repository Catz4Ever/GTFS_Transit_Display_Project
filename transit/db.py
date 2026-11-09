import psycopg
 
 
def connect_db(db_config: dict):
    return psycopg.connect(**db_config, autocommit=True)
 