import socket
import logging
from zeroconf import ServiceInfo, Zeroconf

logger = logging.getLogger(__name__)

def get_local_ip():
    """Get the active local IP address of this computer."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable, just triggers the OS routing table lookup
        s.connect(('10.254.254.254', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

class HubDiscovery:
    def __init__(self, port: int, name: str = "FFmpegHub"):
        self.port = port
        self.name = name
        self.zeroconf = None
        self.service_info = None

    def start(self):
        """Register the service in the local network."""
        local_ip = get_local_ip()
        logger.info(f"Starting mDNS service on {local_ip}:{self.port}")
        
        # Define service properties
        desc = {'version': '1.0'}
        
        # Service names in Zeroconf must end with the type, domain, and a dot
        service_type = "_ffmpeg-hub._tcp.local."
        service_name = f"{self.name}.{service_type}"
        
        try:
            self.service_info = ServiceInfo(
                type_=service_type,
                name=service_name,
                addresses=[socket.inet_aton(local_ip)],
                port=self.port,
                properties=desc,
                server=f"{self.name}.local."
            )
            self.zeroconf = Zeroconf()
            self.zeroconf.register_service(self.service_info)
            logger.info("mDNS service registered successfully.")
        except Exception as e:
            logger.error(f"Failed to register mDNS service: {e}")

    def stop(self):
        """Unregister the service."""
        if self.zeroconf and self.service_info:
            logger.info("Stopping mDNS service...")
            try:
                self.zeroconf.unregister_service(self.service_info)
                self.zeroconf.close()
                logger.info("mDNS service unregistered.")
            except Exception as e:
                logger.error(f"Error during mDNS unregistration: {e}")
            self.zeroconf = None
            self.service_info = None

if __name__ == "__main__":
    # Test block
    import time
    logging.basicConfig(level=logging.INFO)
    discovery = HubDiscovery(8000)
    discovery.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        discovery.stop()
