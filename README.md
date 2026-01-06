# ShieldPi

**Professional Backup System for Docker and Raspberry Pi.**

ShieldPi is a robust tool built to protect your containers and critical files. It combines the power of **Kopia** (encryption and deduplication) with an intuitive visual dashboard and full control over the Docker environment.

## Key Features

* **High Security:** Encrypted repository and password-protected web access.
* **Docker Link:** Links folders with containers. ShieldPi automatically pauses the container before restoring and restarts it upon completion, preventing database corruption.
* **Scheduled Tasks:** Execution of daily backups with local time adjustment.
* **History Control:** Define the number of copies to keep (1 to 10) to save disk space.
* **Alerts & Notifications:** Receive real-time messages via Telegram, Discord, or Webhooks.
* **Visual Recovery:** Browse history and restore with a single click.

## Quick Deployment (Docker Compose)

Copy this code into a file named `docker-compose.yml` to install:

```yaml

services:
  shieldpi:
    image: syncwizards/shieldpi:latest
    container_name: shieldpi
    restart: unless-stopped
    ports:
      - 51515:51515
    environment:
      - TZ=  # Adjust to your Timezone
    volumes:
      # Settings and Database
      - ./shieldpi_data:/app/config
      # Access to Host to backup files
      - /:/host
      # Docker Control (To stop/start containers)
      - /var/run/docker.sock:/var/run/docker.sock
    privileged: true

Quick Start
1.	Run docker-compose up -d.
2.	Access http://YOUR-IP:51515.
3.	Create your administrator user and password.
4.	Initialize the repository (choose a local folder, e.g., /host/backups).
5.	Add your first folder:
o	If it is a database or active app, use the "Link" (Vincular) option to associate it with its Docker container.
6.	Configure alerts in the bell icon.
Requirements
•	Docker and Docker Compose.
•	Access to the Docker socket (/var/run/docker.sock) for container management.
•	It is suggested to run in privileged: true mode to ensure read access to all host system folders.
Contribute
This project is open source. Suggestions and improvements are welcome!
________________________________________
Developed by SyncWizards for the Self-Hosted community.