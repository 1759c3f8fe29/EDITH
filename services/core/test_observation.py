from nexus.core.events import bus
from nexus.monitors import supervisor
from nexus.core import proactive
import asyncio

async def monitor_events():
    agen = bus.subscribe(topics=('observation',))
    print('Subscribed to observation events', flush=True)
    try:
        async for event in agen:
            print(f'Event: {event.type}', flush=True)
            if event.type.startswith('observation.'):
                print(f'  Kind: {event.payload.get("kind")}', flush=True)
                print(f'  Summary: {event.payload.get("summary")}', flush=True)
    except asyncio.CancelledError:
        pass

async def main():
    # Start monitors
    sup = supervisor()
    print('Starting monitors...', flush=True)
    await sup.start()
    print('Monitors started', flush=True)

    # Start proactive
    proactive.start()
    print('Proactive started', flush=True)

    # Monitor events
    task = asyncio.create_task(monitor_events())
    await asyncio.sleep(15)
    print('Proactive status:', proactive.status(), flush=True)
    task.cancel()
    await task

asyncio.run(main())