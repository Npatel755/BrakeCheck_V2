# N42 voice and delivery split

Automatic emergency-contact messages remain on the connected backend and
Photon agent. iOS Messages requires the user to approve a composed message,
so a phone-only app cannot provide the existing no-response alert behavior.
The backend still owns the deadline, contact consent, deduplication, and
delivery status. It can now be configured without an ElevenLabs key.

The iPhone now speaks real and demo check-in prompts with its device voice.
Where Speech supports on-device recognition for the selected language, the
app records a six-second reply and classifies it on the phone. A clear help
or okay response is sent as a decision to the backend for a real check-in;
recorded audio is not uploaded. Help and negation take priority over okay;
unclear speech leaves the buttons and deadline active. A real impact never
opens a permission prompt during its countdown: voice recognition runs only
when microphone and Speech access were granted beforehand. The prompt ends
before the microphone starts. Screen buttons work without Speech support.

The rehearsal now runs entirely on the phone without pairing or messages.
It stops safely if the app leaves the foreground. ElevenLabs brake audio is
optional and defaults off on new installations; existing saved choices remain.
The device voice always remains the brake-alert fallback.
An unconfirmed or partially accepted contact alert now shows a warning icon
instead of a success checkmark on the resolved check-in screen.

Validation: 418 Core tests passed, including conservative reply-classifier
cases. The backend suite passed 53 tests, and blank-ElevenLabs configuration
was exercised against health and crash routes. The N42 release IPA compiled,
passed archive CRC, and passed bundled detector, UFLD, and SegFormer hash
checks. On-device permission, speech accuracy, and end-to-end Photon delivery
still need a real iPhone test.
