"""TCP Load Balancer for distributing connections across multiple bridges."""

from __future__ import annotations

import asyncio
import random
import time
from typing import List, Optional, Dict
from collections import defaultdict

from ..models.proxy import BridgeRuntime


class BridgeLoadBalancer:
    """TCP load balancer that distributes connections across multiple bridges."""
    
    def __init__(self, bridges: List[BridgeRuntime], port: int, strategy: str = 'random'):
        """Initialize the load balancer.
        
        Args:
            bridges: List of bridge runtimes to distribute connections across
            port: Port to listen on for incoming connections
            strategy: Selection strategy ('random', 'round-robin', 'least-conn')
        """
        self._bridges = bridges
        self._port = port
        self._strategy = strategy
        self._server: Optional[asyncio.Server] = None
        self._active = False
        
        # Statistics
        self._total_connections = 0
        self._active_connections = 0
        self._connections_per_bridge: Dict[int, int] = defaultdict(int)
        self._active_per_bridge: Dict[int, int] = defaultdict(int)
        
        # Round-robin state
        self._round_robin_index = 0
    
    async def start(self) -> None:
        """Start the load balancer server."""
        if self._active:
            return
        
        self._server = await asyncio.start_server(
            self._handle_client,
            '127.0.0.1',
            self._port,
            reuse_address=True
        )
        self._active = True
    
    async def stop(self) -> None:
        """Stop the load balancer server."""
        if not self._active or not self._server:
            return
        
        self._active = False  # Set this first to prevent new connections
        
        try:
            self._server.close()
            await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
        except asyncio.TimeoutError:
            # Force close if timeout
            pass
        except Exception:
            # Ignore other errors during shutdown
            pass
    
    def _select_bridge(self) -> Optional[BridgeRuntime]:
        """Select a bridge based on the configured strategy.
        
        This method is called for EVERY new TCP connection.
        In 'random' mode, each connection gets a different random proxy.
        """
        if not self._bridges:
            return None
        
        if self._strategy == 'random':
            # IMPORTANT: Each connection/request gets a NEW random bridge
            # This ensures true load distribution across all proxies
            return random.choice(self._bridges)
        
        elif self._strategy == 'round-robin':
            # Sequential distribution: bridge 0, 1, 2, 0, 1, 2, ...
            bridge = self._bridges[self._round_robin_index]
            self._round_robin_index = (self._round_robin_index + 1) % len(self._bridges)
            return bridge
        
        elif self._strategy == 'least-conn':
            # Select bridge with least active connections for better load balancing
            min_conns = min(self._active_per_bridge.get(i, 0) for i in range(len(self._bridges)))
            candidates = [
                i for i in range(len(self._bridges))
                if self._active_per_bridge.get(i, 0) == min_conns
            ]
            bridge_idx = random.choice(candidates)
            return self._bridges[bridge_idx]
        
        # Default to random
        return random.choice(self._bridges)
    
    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming client connection.
        
        This is called for EVERY new TCP connection to the load balancer.
        Each connection selects a bridge based on the strategy:
        - 'random': New random proxy for each request
        - 'round-robin': Sequential rotation
        - 'least-conn': Least loaded bridge
        """
        # Check if load balancer is still active
        if not self._active:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            return
        
        # Select bridge for THIS connection (new selection per request)
        bridge = self._select_bridge()
        if not bridge:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            return
        
        # Get bridge index for statistics
        try:
            bridge_idx = self._bridges.index(bridge)
        except ValueError:
            bridge_idx = 0
        
        # Update statistics
        self._total_connections += 1
        self._active_connections += 1
        self._connections_per_bridge[bridge_idx] += 1
        self._active_per_bridge[bridge_idx] += 1
        
        bridge_reader: Optional[asyncio.StreamReader] = None
        bridge_writer: Optional[asyncio.StreamWriter] = None
        relay_task1: Optional[asyncio.Task] = None
        relay_task2: Optional[asyncio.Task] = None
        
        try:
            # Connect to the selected bridge
            bridge_reader, bridge_writer = await asyncio.open_connection(
                '127.0.0.1',
                bridge.port
            )
            
            # Create relay tasks
            relay_task1 = asyncio.create_task(
                self._relay(client_reader, bridge_writer, 'client->bridge')
            )
            relay_task2 = asyncio.create_task(
                self._relay(bridge_reader, client_writer, 'bridge->client')
            )
            
            # Wait for both to complete
            await asyncio.gather(relay_task1, relay_task2, return_exceptions=True)
        
        except asyncio.CancelledError:
            # Task was cancelled, cancel relay tasks
            if relay_task1 and not relay_task1.done():
                relay_task1.cancel()
                try:
                    await relay_task1
                except asyncio.CancelledError:
                    pass
            if relay_task2 and not relay_task2.done():
                relay_task2.cancel()
                try:
                    await relay_task2
                except asyncio.CancelledError:
                    pass
            raise
        
        except Exception:
            # Connection failed, cancel any pending relay tasks
            if relay_task1 and not relay_task1.done():
                relay_task1.cancel()
                try:
                    await relay_task1
                except (asyncio.CancelledError, Exception):
                    pass
            if relay_task2 and not relay_task2.done():
                relay_task2.cancel()
                try:
                    await relay_task2
                except (asyncio.CancelledError, Exception):
                    pass
        
        finally:
            # Update statistics
            self._active_connections -= 1
            self._active_per_bridge[bridge_idx] -= 1
            
            # Close connections gracefully
            if bridge_writer:
                try:
                    bridge_writer.close()
                    await bridge_writer.wait_closed()
                except Exception:
                    pass
            
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
    
    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        direction: str
    ) -> None:
        """Relay data from reader to writer.
        
        Args:
            reader: Source stream reader
            writer: Destination stream writer
            direction: Direction label for debugging
        """
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                
                writer.write(data)
                await writer.drain()
        
        except asyncio.CancelledError:
            # Task was cancelled, stop relay cleanly
            raise
        
        except (ConnectionResetError, BrokenPipeError, OSError):
            # Connection closed, stop relay
            pass
        
        except Exception:
            # Other errors, stop relay
            pass
        
        finally:
            # Try to close writer gracefully
            try:
                if not writer.is_closing():
                    writer.close()
            except Exception:
                pass
    
    @property
    def is_active(self) -> bool:
        """Check if load balancer is active."""
        return self._active
    
    @property
    def port(self) -> int:
        """Get the listening port."""
        return self._port
    
    @property
    def strategy(self) -> str:
        """Get the selection strategy."""
        return self._strategy
    
    @property
    def total_connections(self) -> int:
        """Get total connections handled."""
        return self._total_connections
    
    @property
    def active_connections(self) -> int:
        """Get currently active connections."""
        return self._active_connections
    
    def get_bridge_stats(self) -> Dict[int, Dict[str, int]]:
        """Get statistics per bridge."""
        stats = {}
        for i in range(len(self._bridges)):
            stats[i] = {
                'total': self._connections_per_bridge.get(i, 0),
                'active': self._active_per_bridge.get(i, 0)
            }
        return stats
    
    def reset_stats(self) -> None:
        """Reset all statistics."""
        self._total_connections = 0
        self._connections_per_bridge.clear()
