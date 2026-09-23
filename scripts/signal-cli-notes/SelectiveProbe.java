// Offline integration probe: executes the real upstream handler with a local-only attachment store.
// No Signal account, encryption keys or network downloads are used.
import java.io.File;
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
        int downloads;
        int lazyDownloads;
        final Path directory;
        boolean materialise;
        final AttachmentHelper attachments;
        Fixture() throws Exception {
            super(null, null, null, null, null, null);
            directory = Files.createTempDirectory("selective-note-");
            attachments = new AttachmentHelper(this) {
                public void downloadAttachment(SignalServiceAttachment attachment) {
                    downloads++;
                    try {
                        var file = getAttachmentFile(attachment.asPointer()).toPath();
                        if (attachment.getContentType().equals("text/x-signal-plain")) Files.writeString(file, BODY);
                        else Files.write(file, materialise ? MEDIA : new byte[0]);
                    } catch (Exception e) { throw new AssertionError(e); }
                }
                public File getAttachmentFile(SignalServiceAttachmentPointer pointer) { return directory.resolve(pointer.getRemoteId().toString()).toFile(); }
                public void retrieveAttachment(SignalServiceAttachment attachment, AttachmentHandler handler) {
                    lazyDownloads++;
                    throw new AssertionError("Unexpected lazy attachment download");
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
            check(fixture.downloads == (selective ? 0 : 200), "Unrelated direct-message media");
            int unrelated = fixture.downloads;
            var group = SignalServiceGroup.newBuilder(SignalServiceGroup.Type.DELIVER).withId(new byte[16]).build();
            var groupMedia = SignalServiceDataMessage.newBuilder().withTimestamp(2).asGroupMessage(group)
                .withAttachments(List.of(attachment("image/jpeg"))).build();
            method.invoke(handler, groupMedia, true, self, self, config);
            check(fixture.downloads == unrelated + (selective ? 0 : 1), "Group media must not match self destination");
            var groupV2 = SignalServiceGroupV2.newBuilder(new GroupMasterKey(new byte[32])).withRevision(1).build();
            var modernGroupMedia = SignalServiceDataMessage.newBuilder().withTimestamp(2).asGroupMessage(groupV2)
                .withAttachments(List.of(attachment("image/jpeg"))).build();
            method.invoke(handler, modernGroupMedia, true, self, null, config);
            check(fixture.downloads == unrelated + (selective ? 0 : 2), "Modern group media must be skipped");
            int beforeNotes = fixture.downloads;
            fixture.materialise = true;
            for (int albumSize : new int[]{1, 10, 16, 30}) {
                var photos = new ArrayList<SignalServiceAttachment>();
                for (int i=0; i<albumSize; i++) photos.add(attachment(i % 5 == 0 ? "video/mp4" : "image/jpeg"));
                photos.add(attachment("text/x-signal-plain"));
                var album = SignalServiceDataMessage.newBuilder().withTimestamp(3).withBody("short caption")
                    .withAttachments(photos).build();
                int beforeNote = fixture.downloads;
                var result = (Pair<?, ?>) method.invoke(handler, album, true, self, self, config);
                check(fixture.downloads - beforeNote == albumSize + 1, "Complete mixed-media album");
                check(((Map<?, ?>) result.second()).containsValue(BODY), "Full attachment-backed note text retained");
                for (int i=0; i<albumSize; i++) {
                    var bytes = Files.readAllBytes(fixture.getAttachmentHelper().getAttachmentFile(photos.get(i).asPointer()).toPath());
                    check(Arrays.equals(bytes, MEDIA), "Every media file preserved byte-for-byte");
                }
                var textOnly = SignalServiceDataMessage.newBuilder().withTimestamp(4).withBody(BODY).build();
                method.invoke(handler, textOnly, true, self, self, config);
                check(fixture.downloads == beforeNote + albumSize + 1, "Text-only note adds no downloads");
                System.out.printf("%s: album=%d media_bytes=%d text_chars=%d full_text=true PASS%n",
                    args[0], albumSize, (long)albumSize * MEDIA.length, BODY.length());
            }
            System.out.printf("%s: unrelated_download_calls=%d PASS%n", args[0], beforeNotes);
            if (selective) {
                var selfAlias = ctor.newInstance(selfRecipient, ServiceId.PNI.parseOrThrow("00000000-0000-0000-0000-000000000003"), 1);
                int beforeAlias = fixture.downloads;
                method.invoke(handler, media, true, self, selfAlias, config);
                check(fixture.downloads == beforeAlias + 1, "Canonical self identity retains its media across service IDs");
                int before = fixture.downloads;
                method.invoke(handler, media, true, self, null, config);
                check(fixture.downloads == before, "Missing destination must not download");
                var unknown = ctor.newInstance(null, null, 1);
                method.invoke(handler, media, true, unknown, unknown, config);
                check(fixture.downloads == before, "Missing identifiers must not match each other");
                var longUnrelated = SignalServiceDataMessage.newBuilder().withTimestamp(5)
                    .withAttachments(List.of(attachment("text/x-signal-plain"))).build();
                method.invoke(handler, longUnrelated, true, self, other,
                    new ReceiveConfig(true, true, true, true, false));
                check(fixture.lazyDownloads == 0, "Unrelated long-text fallback bypassed");
                System.out.println("selective: missing_destination_skipped=true unrelated_long_text_skipped=true PASS");
            }
        } finally { fixture.clean(); }
    }
}
