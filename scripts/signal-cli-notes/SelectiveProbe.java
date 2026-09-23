// Offline integration probe: executes the real upstream handler with a local-only attachment store.
// No Signal account, encryption keys or network downloads are used.
import java.io.File;
import java.io.IOException;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.asamk.signal.manager.storage.AttachmentStore;
import java.nio.file.*;
import java.lang.reflect.*;
import java.util.*;
import org.asamk.signal.manager.api.*;
import org.asamk.signal.manager.helper.*;
import org.asamk.signal.manager.storage.groups.GroupInfo;
import org.asamk.signal.manager.storage.groups.GroupInfoV2;
import org.signal.libsignal.zkgroup.groups.GroupMasterKey;
import org.asamk.signal.manager.storage.recipients.RecipientId;
import org.signal.core.models.ServiceId;
import org.whispersystems.signalservice.api.messages.*;

public class SelectiveProbe {
    static final String BODY = "Long draft: café, 中文, 🦷\nSecond paragraph.\n".repeat(12000);
    static final byte[] MEDIA = new byte[3 * 1024 * 1024];
    static int attachmentId;
    static { Arrays.fill(MEDIA, (byte) 73); }
    static class Fixture extends Context {
        final AtomicInteger downloads = new AtomicInteger();
        final AtomicInteger active = new AtomicInteger();
        final AtomicInteger peak = new AtomicInteger();
        final List<String> completed = Collections.synchronizedList(new ArrayList<>());
        final Set<String> failures = new HashSet<>();
        final Path directory;
        boolean materialise;
        int delayMs;
        String slowId;
        CountDownLatch entered;
        CountDownLatch release;
        final AttachmentHelper attachments;
        Fixture() throws Exception { this(Files.createTempDirectory("selective-note-")); }
        Fixture(Path directory) throws Exception {
            super(null, null, null, null, new AttachmentStore(directory.toFile()), null);
            this.directory = directory;
            attachments = new AttachmentHelper(this) {
                // Exercise upstream downloadAttachment, AttachmentStore and copy/cleanup;
                // replace only encrypted network retrieval with disposable fixture bytes.
                public void retrieveAttachment(SignalServiceAttachment attachment, AttachmentHandler consumer) throws IOException {
                    downloads.incrementAndGet();
                    peak.accumulateAndGet(active.incrementAndGet(), Math::max);
                    String id = attachment.asPointer().getRemoteId().toString();
                    try {
                        if (entered != null) {
                            entered.countDown();
                            if (!release.await(5, TimeUnit.SECONDS)) throw new AssertionError("Download gate timed out");
                        }
                        Thread.sleep(delayMs + (id.equals(slowId) ? 200 : 0));
                        if (failures.contains(id)) throw new IOException("Simulated attachment failure");
                        byte[] bytes = attachment.getContentType().equals("text/x-signal-plain")
                                ? BODY.getBytes(StandardCharsets.UTF_8)
                                : (materialise ? mediaBytes(attachment) : new byte[0]);
                        consumer.handle(new ByteArrayInputStream(bytes));
                        completed.add(id);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        throw new IOException("Interrupted fixture download", e);
                    } finally { active.decrementAndGet(); }
                }
            };
        }
        public AttachmentHelper getAttachmentHelper() { return attachments; }
        public ContactHelper getContactHelper() {
            return new ContactHelper(null) {
                public void setExpirationTimer(RecipientId id, int seconds, int version) {}
            };
        }
        public GroupHelper getGroupHelper() {
            return new GroupHelper(this) {
                public GroupInfo getGroup(GroupId id) { return null; }
                public GroupInfoV2 getOrMigrateGroup(GroupMasterKey key, int revision, byte[] change, boolean ignoreAvatar) { return null; }
            };
        }
        void clean() throws Exception {
            getJobExecutor().close();
            try (var files = Files.list(directory)) {
                for (var file : files.toList()) Files.delete(file);
            }
            Files.delete(directory);
        }
    }
    static byte[] mediaBytes(SignalServiceAttachment attachment) {
        byte[] bytes = MEDIA.clone();
        byte[] id = attachment.asPointer().getRemoteId().toString().getBytes(StandardCharsets.UTF_8);
        System.arraycopy(id, 0, bytes, 0, id.length);
        return bytes;
    }
    static SignalServiceAttachmentPointer attachment(String mime) {
        return new SignalServiceAttachmentPointer(0, SignalServiceAttachmentRemoteId.from(Integer.toString(++attachmentId), 0), mime,
            new byte[64], Optional.of(MEDIA.length), Optional.empty(), 0, 0, Optional.empty(), Optional.empty(),
            0, Optional.empty(), false, false, false, Optional.empty(), Optional.empty(), 0, UUID.randomUUID());
    }
    static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    public static void main(String[] args) throws Exception {
        boolean selective = args[0].equals("selective");
        var fixture = new Fixture();
        try {
            var handler = new IncomingMessageHandler(fixture);
            var addressClass = Class.forName("org.asamk.signal.manager.helper.IncomingMessageHandler$DeviceAddress");
            var ctor = addressClass.getDeclaredConstructors()[0];
            ctor.setAccessible(true);
            var selfId = ServiceId.ACI.parseOrThrow("00000000-0000-0000-0000-000000000001");
            var otherId = ServiceId.ACI.parseOrThrow("00000000-0000-0000-0000-000000000002");
            var recipientCtor = RecipientId.class.getDeclaredConstructors()[0];
            recipientCtor.setAccessible(true);
            var selfRecipient = recipientCtor.newInstance(1L, null);
            var otherRecipient = recipientCtor.newInstance(2L, null);
            var self = ctor.newInstance(selfRecipient, selfId, 1);
            var other = ctor.newInstance(otherRecipient, otherId, 1);
            var method = IncomingMessageHandler.class.getDeclaredMethod("handleSignalServiceDataMessage",
                SignalServiceDataMessage.class, boolean.class, addressClass, addressClass, ReceiveConfig.class);
            method.setAccessible(true);
            var config = new ReceiveConfig(false, true, true, true, false);
            var media = SignalServiceDataMessage.newBuilder().withTimestamp(1)
                .withAttachments(List.of(attachment("video/mp4"))).build();
            for (int i=0; i<100; i++) {
                method.invoke(handler, media, false, other, self, config);
                method.invoke(handler, media, true, self, other, config);
            }
            check(fixture.downloads.get() == (selective ? 0 : 200), "Unrelated direct-message media");
            int unrelated = fixture.downloads.get();
            var group = SignalServiceGroup.newBuilder(SignalServiceGroup.Type.DELIVER).withId(new byte[16]).build();
            var groupMedia = SignalServiceDataMessage.newBuilder().withTimestamp(2).asGroupMessage(group)
                .withAttachments(List.of(attachment("image/jpeg"))).build();
            method.invoke(handler, groupMedia, true, self, self, config);
            check(fixture.downloads.get() == unrelated + (selective ? 0 : 1), "Group media must not match self destination");
            var groupV2 = SignalServiceGroupV2.newBuilder(new GroupMasterKey(new byte[32])).withRevision(1).build();
            var modernGroupMedia = SignalServiceDataMessage.newBuilder().withTimestamp(2).asGroupMessage(groupV2)
                .withAttachments(List.of(attachment("image/jpeg"))).build();
            method.invoke(handler, modernGroupMedia, true, self, null, config);
            check(fixture.downloads.get() == unrelated + (selective ? 0 : 2), "Modern group media must be skipped");
            int beforeNotes = fixture.downloads.get();
            fixture.materialise = true;
            for (int albumSize : new int[]{1, 10, 16, 30}) {
                var photos = new ArrayList<SignalServiceAttachment>();
                for (int i=0; i<albumSize; i++) photos.add(attachment(i % 5 == 0 ? "video/mp4" : "image/jpeg"));
                photos.add(attachment("text/x-signal-plain"));
                var album = SignalServiceDataMessage.newBuilder().withTimestamp(3).withBody("short caption")
                    .withAttachments(photos).build();
                int beforeNote = fixture.downloads.get();
                var result = (Pair<?, ?>) method.invoke(handler, album, true, self, self, config);
                check(fixture.downloads.get() - beforeNote == albumSize + 1, "Complete mixed-media album");
                check(((Map<?, ?>) result.second()).containsValue(BODY), "Full attachment-backed note text retained");
                for (int i=0; i<albumSize; i++) {
                    var bytes = Files.readAllBytes(fixture.getAttachmentHelper().getAttachmentFile(photos.get(i).asPointer()).toPath());
                    check(Arrays.equals(bytes, mediaBytes(photos.get(i))), "Every media file preserved byte-for-byte");
                }
                var textOnly = SignalServiceDataMessage.newBuilder().withTimestamp(4).withBody(BODY).build();
                method.invoke(handler, textOnly, true, self, self, config);
                check(fixture.downloads.get() == beforeNote + albumSize + 1, "Text-only note adds no downloads");
                System.out.printf("%s: album=%d media_bytes=%d text_chars=%d full_text=true PASS%n",
                    args[0], albumSize, (long)albumSize * MEDIA.length, BODY.length());
            }
            System.out.printf("%s: unrelated_download_calls=%d PASS%n", args[0], beforeNotes);
            var timedAttachments = new ArrayList<SignalServiceAttachment>();
            for (int i=0; i<24; i++) timedAttachments.add(attachment("image/jpeg"));
            var timedAlbum = SignalServiceDataMessage.newBuilder().withTimestamp(6).withAttachments(timedAttachments).build();
            fixture.materialise = false;
            fixture.delayMs = 80;
            fixture.peak.set(0);
            long started = System.nanoTime();
            method.invoke(handler, timedAlbum, true, self, self, config);
            long elapsed = (System.nanoTime() - started) / 1_000_000;
            check(fixture.active.get() == 0, "No unfinished downloads after handler returns");
            check(fixture.peak.get() == (selective ? 4 : 1), "Download concurrency stays bounded");
            System.out.printf("%s: benchmark_files=24 delay_ms=80 elapsed_ms=%d peak=%d PASS%n",
                args[0], elapsed, fixture.peak.get());
            fixture.delayMs = 0;
            if (selective) {
                // Force out-of-order completion without changing album order or file contents.
                fixture.materialise = true;
                fixture.completed.clear();
                fixture.slowId = timedAttachments.getFirst().asPointer().getRemoteId().toString();
                method.invoke(handler, timedAlbum, true, self, self, config);
                check(!fixture.completed.getFirst().equals(fixture.slowId), "Fixture completes out of order");
                check(timedAlbum.getAttachments().get().equals(timedAttachments), "Original attachment order retained");
                for (var item : timedAttachments) {
                    check(Arrays.equals(Files.readAllBytes(fixture.attachments.getAttachmentFile(item.asPointer()).toPath()),
                        mediaBytes(item)), "Out-of-order downloads retain the correct bytes");
                }
                fixture.slowId = null;

                // Duplicate pointers share a file and must not be written concurrently.
                var repeated = attachment("image/jpeg");
                var duplicateAlbum = SignalServiceDataMessage.newBuilder().withTimestamp(7)
                    .withAttachments(List.of(repeated, repeated, attachment("video/mp4"))).build();
                fixture.peak.set(0);
                fixture.delayMs = 10;
                method.invoke(handler, duplicateAlbum, true, self, self, config);
                check(fixture.peak.get() == 1, "Repeated output paths fall back to serial downloads");
                fixture.delayMs = 0;

                // Real upstream helper reports an I/O failure but continues the other files.
                var failed = attachment("image/jpeg");
                var kept = attachment("video/mp4");
                var text = attachment("text/x-signal-plain");
                fixture.failures.add(failed.getRemoteId().toString());
                var failedAlbum = SignalServiceDataMessage.newBuilder().withTimestamp(8)
                    .withAttachments(List.of(failed, kept, text)).build();
                var failedResult = (Pair<?, ?>) method.invoke(handler, failedAlbum, true, self, self, config);
                check(((Map<?, ?>) failedResult.second()).containsValue(BODY), "Failed photo cannot truncate body");
                check(Arrays.equals(Files.readAllBytes(fixture.attachments.getAttachmentFile(kept).toPath()),
                    mediaBytes(kept)), "Successful media retained after another download fails");
                check(fixture.active.get() == 0, "Failed album leaves no running download workers");
                fixture.failures.clear();

                // Hold four transfers open: handler must not publish a partial note.
                fixture.entered = new CountDownLatch(4);
                fixture.release = new CountDownLatch(1);
                try (var caller = Executors.newSingleThreadExecutor()) {
                    var pending = caller.submit(() -> method.invoke(handler, timedAlbum, true, self, self, config));
                    try {
                        check(fixture.entered.await(5, TimeUnit.SECONDS), "Four downloads started together");
                        check(!pending.isDone(), "Handler waits for every download before returning");
                    } finally { fixture.release.countDown(); }
                    pending.get(10, TimeUnit.SECONDS);
                }
                fixture.entered = fixture.release = null;
                check(fixture.active.get() == 0, "All workers complete before note emission");

                fixture.entered = new CountDownLatch(4);
                fixture.release = new CountDownLatch(1);
                var interrupted = new AtomicReference<Throwable>();
                var caller = new Thread(() -> {
                    try {
                        method.invoke(handler, timedAlbum, true, self, self, config);
                        interrupted.set(new AssertionError("Interrupted receive must not return a partial note"));
                    } catch (InvocationTargetException e) {
                        if (!(e.getCause() instanceof IllegalStateException) || !Thread.currentThread().isInterrupted()) {
                            interrupted.set(e);
                        }
                    } catch (Exception e) { interrupted.set(e); }
                });
                caller.start();
                try {
                    check(fixture.entered.await(5, TimeUnit.SECONDS), "Cancellation fixture started four transfers");
                    caller.interrupt();
                    caller.join(5000);
                    check(!caller.isAlive(), "Cancelled handler exits promptly");
                    check(interrupted.get() == null, "Cancellation preserves interrupt and fails the envelope");
                    check(fixture.active.get() == 0, "Cancellation leaves no running download workers");
                } finally {
                    fixture.release.countDown();
                    caller.join(5000);
                    fixture.entered = fixture.release = null;
                }
                System.out.println("selective: ordering=true duplicate_paths_serial=true failure_isolated=true completion_barrier=true cancellation=true PASS");
                var selfAlias = ctor.newInstance(selfRecipient, ServiceId.PNI.parseOrThrow("00000000-0000-0000-0000-000000000003"), 1);
                int beforeAlias = fixture.downloads.get();
                method.invoke(handler, media, true, self, selfAlias, config);
                check(fixture.downloads.get() == beforeAlias + 1, "Canonical self identity retains its media across service IDs");
                int before = fixture.downloads.get();
                method.invoke(handler, media, true, self, null, config);
                check(fixture.downloads.get() == before, "Missing destination must not download");
                var unknown = ctor.newInstance(null, null, 1);
                method.invoke(handler, media, true, unknown, unknown, config);
                check(fixture.downloads.get() == before, "Missing identifiers must not match each other");
                var longUnrelated = SignalServiceDataMessage.newBuilder().withTimestamp(5)
                    .withAttachments(List.of(attachment("text/x-signal-plain"))).build();
                method.invoke(handler, longUnrelated, true, self, other,
                    new ReceiveConfig(true, true, true, true, false));
                check(fixture.downloads.get() == before, "Unrelated long-text fallback bypassed");
                System.out.println("selective: missing_destination_skipped=true unrelated_long_text_skipped=true PASS");
            }
        } finally { fixture.clean(); }
    }
}
