from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL ="postgresql://postgres:masi123!@localhost:5432/fireflux" 
engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine)