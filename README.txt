# Вдигане на контейнера
docker compose build
# Създаване на базата данни
docker compose up -d --build db 
# пускане на Schema
Get-Content .\schema.sql | docker compose exec -T db psql -U transit_user -d transit_db -v ON_ERROR_STOP=1
# пускане на другите подконтейнери
docker compose up -d --build db api runtime
# После се посещава localhost:8000/admin и от .env се взима тайният token
# В .zip файла е добавен Mosquitto брокера. Поставя се в Root-а на C и се пуска чрез:
cd 'C:\Program Files\mosquitto'
.\mosquitto.exe -c C:\mosquitto\mosquitto.conf