from pathlib import Path
import difflib
import shutil
base=Path('/workspace/vm-g-failure-shard')
candidate=Path('/workspace/vm-g-artifacts/candidate')
candidate.mkdir(exist_ok=True)
shutil.copytree(base/'src',candidate/'src',dirs_exist_ok=True)
service='src/flowmarshal/engine/service.py'
runtime='src/flowmarshal/engine/runtime.py'
old=(base/service).read_text()
addition='''    def _claim_runtime_job_interrupt_delivery(
        self, job_id: str, *, thread_id: str, turn_id: str,
    ) -> dict[str, Any] | None:
        """exact turn의 interrupt 전달을 원자적으로 한 번 claim한다.

        marker 뒤 receipt 없는 중단은 전달 여부 불명이다. 재시작은 원래 turn을
        관측하고 같은 interrupt를 재전송하지 않는다. binding 전 요청은 claim하지 않는다.
        """
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM runtime_jobs WHERE id=?", (job_id,))
            if (
                (row["thread_id"], row["turn_id"]) != (thread_id, turn_id)
                or row["status"] not in {
                    RuntimeJobStatus.INTERRUPTING.value,
                    RuntimeJobStatus.CANCELLED.value,
                }
                or tx.maybe_one(
                    "SELECT 1 FROM runtime_job_observations WHERE job_id=? AND kind=? LIMIT 1",
                    (job_id, RuntimeJobObservationKind.INTERRUPT_RECEIPT.value),
                ) is not None
                or tx.maybe_one(
                    "SELECT 1 FROM history_events WHERE entity_type='runtime_job' AND entity_id=? "
                    "AND event_type='runtime_job.interrupt_dispatching' LIMIT 1",
                    (job_id,),
                ) is not None
            ):
                return None
            request = tx.maybe_one(
                "SELECT id,payload_json FROM runtime_job_observations WHERE job_id=? AND kind=? "
                "ORDER BY rowid DESC LIMIT 1",
                (job_id, RuntimeJobObservationKind.INTERRUPT_REQUESTED.value),
            )
            if request is None:
                raise EngineServiceError("RUNTIME_INTERRUPT_REQUEST_MISSING")
            tx.history(
                row["project_id"], "runtime_job.interrupt_dispatching", "runtime_job", job_id,
                {"request_observation_id": request["id"], "thread_id": thread_id, "turn_id": turn_id},
            )
            return json.loads(request["payload_json"])

'''
new=old.replace('    def consume_runtime_job(self, job_id: str)',addition+'    def consume_runtime_job(self, job_id: str)',1)
(candidate/service).write_text(new)
r_old=(base/runtime).read_text()
needle='''        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM runtime_job_observations "
                "WHERE job_id=? AND kind=? ORDER BY rowid DESC LIMIT 1",
                (job.job_id, RuntimeJobObservationKind.INTERRUPT_REQUESTED.value),
            ).fetchone()
        if row is None:
            raise RuntimePolicyError("RUNTIME_INTERRUPT_REQUEST_MISSING")
        request = json.loads(row["payload_json"])
'''
replacement='''        request = self.service._claim_runtime_job_interrupt_delivery(
            job.job_id, thread_id=job.thread_id, turn_id=job.turn_id,
        )
        if request is None:
            return
'''
assert r_old.count(needle)==1
r_new=r_old.replace(needle,replacement)
(candidate/runtime).write_text(r_new)
patch=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/'+service,tofile='b/'+service))
patch+=''.join(difflib.unified_diff(r_old.splitlines(True),r_new.splitlines(True),fromfile='a/'+runtime,tofile='b/'+runtime))
Path('/workspace/vm-g-artifacts/interrupt-delivery-proposal.patch').write_text(patch)
