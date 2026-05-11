import os

from qaas.iqm_backend.backend_service import IQMBackendService


def main():
    """Main function to start the IQM Backend Service. It reads configuration from environment variables and initializes the service."""
    socket_path = os.getenv("IQM_SERVICE_SOCKET", "/tmp/iqm_backend.sock")
    work_dir = os.getenv("IQM_WORK_DIR", "/tmp/iqm_tasks")

    print("Starting server...")
    service = IQMBackendService(socket_path, work_dir)
    service.start()


if __name__ == "__main__":
    main()
