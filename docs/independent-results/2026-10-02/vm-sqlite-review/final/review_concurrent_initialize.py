"""동시 최초 초기화 한 시나리오를 20회 반복한다. 80개의 unique test가 아니다."""
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
from review_initialize_v2 import SQLiteEngineLedger, HERE


def worker(path, barrier, queue):
    try:
        class Synced(SQLiteEngineLedger):
            def _connect(self, **options):
                barrier.wait(timeout=10)
                return super()._connect(**options)
        Synced(path).initialize()
        queue.put('ok')
    except BaseException as exc:
        queue.put(type(exc).__name__ + ': ' + str(exc))


def main():
    ctx = mp.get_context('fork'); results = []
    for _ in range(20):
        with tempfile.TemporaryDirectory(prefix='synthetic-init-race-', dir=HERE) as tmp:
            path = str(Path(tmp) / 'fixture.sqlite3')
            barrier = ctx.Barrier(4); queue = ctx.Queue()
            children = [ctx.Process(target=worker, args=(path, barrier, queue)) for _ in range(4)]
            for child in children: child.start()
            outcomes = [queue.get(timeout=20) for _ in children]
            for child in children:
                child.join(10)
                assert child.exitcode == 0, child.exitcode
            results.append(outcomes)
    print(json.dumps(results, indent=2))
    assert all(result == ['ok'] * 4 for result in results), results


if __name__ == '__main__': main()
