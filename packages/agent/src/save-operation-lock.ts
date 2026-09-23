const cleanupInstances = new Set<string>();

export function isSaveCleanupLocked(instanceId: string): boolean {
  return cleanupInstances.has(instanceId);
}

export function acquireSaveCleanupLock(instanceId: string): () => void {
  if (cleanupInstances.has(instanceId)) {
    throw Object.assign(new Error("Save cleanup is already running for this server"), { statusCode: 409 });
  }
  cleanupInstances.add(instanceId);
  return () => cleanupInstances.delete(instanceId);
}
