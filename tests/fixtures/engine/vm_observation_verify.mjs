// V02-d의 별도 Node 프로세스 fixture. AGS V03 제품 verifier가 아니다.
import { createHash, createPublicKey, verify } from 'node:crypto';
import { closeSync, mkdirSync, openSync } from 'node:fs';
import { join } from 'node:path';

const canonical = (value) => {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
};
const decode = (value) => {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) throw Error('base64url');
  const bytes = Buffer.from(value, 'base64url');
  if (bytes.toString('base64url') !== value) throw Error('base64url');
  return bytes;
};
const reject = (reason) => ({ accepted: false, reason });

function check(input) {
  const { receipt, expected, trust, arguments: wireArguments, seen = [], receivedAt, claimDir } = input;
  if (!wireArguments || canonical(wireArguments._hostAttestation) !== canonical(receipt)) {
    return reject('attestation_mismatch');
  }
  const pin = trust?.[receipt?.keyId];
  if (!pin || pin.revoked) return reject('key_not_trusted');
  let bytes, body;
  try {
    bytes = decode(receipt.body);
    body = JSON.parse(bytes.toString('utf8'));
    if (Buffer.from(canonical(body), 'utf8').compare(bytes) !== 0) return reject('noncanonical');
    const key = createPublicKey({ key: Buffer.from(pin.publicKeySpki, 'base64'), format: 'der', type: 'spki' });
    if (!verify(null, bytes, key, decode(receipt.signature))) return reject('signature_invalid');
  } catch {
    return reject('signature_invalid');
  }
  if (body.version !== 1 || body.domain !== 'vm-provider-terminal-to-governance'
      || body.producer?.keyId !== receipt.keyId
      || body.producer?.installationId !== pin.installationId
      || body.producer?.hostId !== pin.hostId
      || body.producer?.hostId !== expected.hostId
      || body.binding?.hostId !== expected.hostId) return reject('host_mismatch');
  for (const field of ['invocationId', 'turnId', 'taskId', 'runId', 'attemptId', 'hostId', 'sessionId', 'instanceId']) {
    if (body.binding?.[field] !== expected.binding[field]) return reject('binding_mismatch');
  }
   const { _hostAttestation, ...unsignedArguments } = wireArguments ?? {};
   const wireDigest = `sha256:${createHash('sha256').update(canonical(unsignedArguments), 'utf8').digest('hex')}`;
   if (body.terminal?.turnId !== body.binding.turnId
       || body.producer.instanceId !== body.binding.instanceId
       || body.invocation?.tool !== expected.tool
       || body.invocation?.inputDigest !== wireDigest) return reject('tool_input_mismatch');
  if (!['completed', 'succeeded'].includes(body.terminal?.status)
      || !['provider_raw_response', 'claude_session_transcript'].includes(body.terminal?.provenance)
      || !body.terminal?.model || !body.terminal?.effort) return reject('terminal_incomplete');
  const issued = Date.parse(body.issuedAt), expiry = Date.parse(body.expiresAt);
  const received = Date.parse(receivedAt);
  if (!(Date.parse(body.terminal.observedAt) < issued && issued === Date.parse(body.invocation.observedAt)
      && expiry - issued === 60000 && issued - received <= 5000 && received < expiry)) return reject('expired');
  const claim = `${receipt.keyId}:${body.nonce}`;
  if (seen.includes(claim)) return reject('already_consumed');
  if (claimDir) {
    mkdirSync(claimDir, { recursive: true });
    try {
      const id = createHash('sha256').update(claim).digest('hex');
      closeSync(openSync(join(claimDir, id), 'wx'));
    } catch (error) {
      if (error.code === 'EEXIST') return reject('already_consumed');
      throw error;
    }
  }
  const hash = createHash('sha256').update(bytes).digest('hex');
  return { accepted: true, observation: {
    binding: body.binding, observationId: `vm-producer-v1:${hash}`,
    observedAt: body.invocation.observedAt, model: body.terminal.model,
    reasoningEffort: body.terminal.effort,
  }, claim };
}

let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(JSON.stringify(check(JSON.parse(source))));
