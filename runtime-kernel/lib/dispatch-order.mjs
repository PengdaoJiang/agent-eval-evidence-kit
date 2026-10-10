// Stable FIFO among eligible durable jobs. Filesystem/hash order is never age.
export const fifo=(a,b)=>(a.createdAt||'').localeCompare(b.createdAt||'')||a.id.localeCompare(b.id);
export const ready=(job,time=Date.now())=>!job.nextAttemptAt||job.nextAttemptAt<=time;
