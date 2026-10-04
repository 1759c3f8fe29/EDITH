You are building NEXUS, a production-oriented JARVIS-style personal AI computer operator.

NEXUS is NOT a generic chatbot.

Its primary purpose is to become an AI operating layer that can understand the user, see the computer, hear the user, reason about goals, remember useful context, operate authorized computer functions, use keyboard and mouse, interact with applications and browsers, execute multi-step tasks, verify its own actions, recover from failures, and communicate naturally through voice and visual interfaces.

IMPORTANT:
Do not claim that NEXUS is scientifically proven human-level AGI. Implement an AGI-STYLE autonomous cognitive architecture with perception, reasoning, planning, memory, tool use, observation, verification, adaptation, and goal-oriented execution.

==================================================
1. PRODUCT VISION
==================================================

Build NEXUS as:

"AI that operates your digital world."

The experience should feel closer to a futuristic JARVIS command system than a conventional AI chat application.

The user should be able to communicate naturally:

"Hey Nexus."

"What is happening on my computer?"

"Open VS Code."

"Find my chemistry notes."

"Why is my computer slow?"

"Look at this screen and tell me what is wrong."

"Prepare my coding environment."

"Search the web for today's AI news."

"Remember this project."

"Forget everything about Project X."

"Every evening remind me to study."

"Close everything except VS Code."

"Investigate why my development server is failing."

NEXUS must understand the user's intent and select appropriate capabilities rather than forcing the user to manually choose tools.

==================================================
2. CORE COGNITIVE ARCHITECTURE
==================================================

Implement a modular cognitive architecture:

USER
↓
VOICE / TEXT / IMAGE
↓
PERCEPTION
↓
CONTEXT ASSEMBLY
↓
INTENT UNDERSTANDING
↓
MEMORY RETRIEVAL
↓
MODEL ROUTER
↓
REASONING
↓
GOAL / TASK PLANNER
↓
TOOL SELECTOR
↓
PERMISSION ENGINE
↓
TOOL EXECUTION
↓
OBSERVATION
↓
VERIFICATION
↓
REPLAN OR CONTINUE
↓
PERSIST STATE
↓
USER FEEDBACK

The central cognitive loop must support:

OBSERVE
→ UNDERSTAND
→ REASON
→ PLAN
→ AUTHORIZE
→ ACT
→ OBSERVE AGAIN
→ VERIFY
→ CORRECT
→ CONTINUE
→ REPORT

Never assume that an action succeeded merely because an action command was sent.

==================================================
3. AGI-STYLE COGNITIVE CAPABILITIES
==================================================

NEXUS must support:

- Goal understanding
- Intent recognition
- Context awareness
- Task decomposition
- Multi-step planning
- Dynamic replanning
- Dependency detection
- Priority management
- Constraint understanding
- Short-term working memory
- Long-term memory
- Project memory
- User preference memory
- Goal memory
- Environmental awareness
- Application awareness
- Screen awareness
- Computer-state awareness
- Tool selection
- Tool chaining
- Result verification
- Error detection
- Failure recovery
- Retry
- Cancellation
- Interruption
- Partial completion
- Resume interrupted work
- State preservation
- Conflict detection
- Decision making
- Source verification
- Output verification
- User clarification when genuinely necessary
- Learning from explicit user corrections
- Privacy-aware context selection
- Local/remote model selection
- Cost-aware model routing where applicable

Do not create fake "self-learning" behavior.

Any learning or adaptation must correspond to real stored preferences, memory, configuration, feedback, or model behavior.

==================================================
4. AUTONOMOUS TASK EXECUTION
==================================================

Support Agent Mode for complex user goals.

Example:

User:
"Prepare my coding environment."

NEXUS should be capable of:

- Understanding the goal
- Inspecting the current computer state
- Determining required applications
- Finding the relevant project
- Opening authorized applications
- Opening the project
- Using keyboard/mouse where required
- Opening a terminal where appropriate
- Starting approved development processes
- Inspecting output
- Detecting errors
- Diagnosing failures
- Correcting problems when authorized
- Re-checking the result
- Reporting completion

The agent must maintain explicit execution state.

Every multi-step task should have:

- Goal
- Current state
- Planned steps
- Completed steps
- Current step
- Failed steps
- Pending steps
- Required permissions
- Observations
- Verification results
- Final result

If the environment changes unexpectedly, do not blindly continue.

Observe the new state and re-plan.

==================================================
5. COMPUTER CONTROL ENGINE
==================================================

NEXUS must be designed around real computer interaction.

Support authorized control of:

- Mouse
- Keyboard
- Windows
- Applications
- Virtual desktops
- Displays
- Terminal
- Files
- Browser
- System information
- Network diagnostics
- Development environments

The computer-control system must prefer semantic/visual targeting over fragile hard-coded screen coordinates whenever possible.

The fundamental interaction pattern is:

OBSERVE SCREEN
→ IDENTIFY TARGET
→ SELECT ACTION
→ EXECUTE
→ OBSERVE RESULT
→ VERIFY

Support:

- Pointer movement
- Left click
- Right click
- Middle click
- Double click
- Drag and drop
- Scrolling
- Keyboard typing
- Key presses
- Keyboard shortcuts
- Text selection
- Copy
- Cut
- Paste
- Undo
- Redo
- Window management
- Application switching
- Terminal interaction
- Screen capture
- Region capture
- UI detection
- Visual verification

==================================================
6. MOUSE SYSTEM
==================================================

Implement a real mouse-control abstraction supporting:

- Pointer position
- Pointer movement
- Target detection
- Click
- Double click
- Right click
- Middle click
- Hold
- Release
- Drag
- Drop
- Vertical scroll
- Horizontal scroll
- Target verification
- Pointer cancellation
- Pointer retry
- Pointer diagnostics
- Pointer permissions

Do not assume fixed coordinates remain valid.

==================================================
7. KEYBOARD SYSTEM
==================================================

Implement a real keyboard-control abstraction supporting:

- Text entry
- Individual key presses
- Modifier keys
- Key combinations
- Shortcuts
- Selection
- Copy
- Cut
- Paste
- Delete
- Enter
- Escape
- Tab
- Arrow keys
- Home
- End
- Page Up
- Page Down
- Function keys
- Undo
- Redo

Before typing into an uncertain location, identify and verify the intended input target.

==================================================
8. SCREEN PERCEPTION
==================================================

NEXUS must be able to inspect the computer visually when supported.

Support:

- Screenshot capture
- Screen capture
- Region capture
- Window capture
- Display capture
- OCR
- Text recognition
- UI-element detection
- Button detection
- Form detection
- Error detection
- Application identification
- Window identification
- Visual state comparison
- Screen-change detection
- Visual troubleshooting
- Visual question answering
- Screen explanation
- Document analysis
- Chart analysis
- Diagram analysis

NEXUS should be able to reason about:

"What am I looking at?"

"What changed?"

"Where is the control I need?"

"Did the previous action succeed?"

==================================================
9. VOICE-FIRST INTERACTION
==================================================

Support:

- "Hey Nexus" wake phrase
- Speech-to-text
- Natural voice conversation
- Text-to-speech
- Voice interruption
- Conversation interruption
- Voice activity detection
- Microphone selection
- Audio-output selection
- Voice selection
- Speech-speed control
- Speech-volume control
- Voice language detection
- Voice command history

Response modes:

- Quick
- Detailed
- Silent

If NEXUS is speaking and the user interrupts, stop or appropriately transition the current speech and process the new input.

==================================================
10. VOICE SPHERE
==================================================

Create a central futuristic Voice Sphere.

The sphere must visually communicate:

- Idle
- Listening
- Thinking
- Speaking
- Interrupted
- Executing
- Waiting for permission
- Error
- Offline
- Completed

The Voice Sphere is a functional interaction surface, not merely decoration.

The visual state must correspond to real application state.

==================================================
11. VISION MODE
==================================================

Provide a dedicated Vision Mode allowing the user to give NEXUS:

- Camera input
- Screenshots
- Screen context
- Images
- Documents

Possible requests:

"What is this?"

"Read this."

"Explain this error."

"What is wrong with my screen?"

"Explain this diagram."

"Compare these two images."

"Explain what I am looking at."

Always clearly indicate when camera/screen access is active.

==================================================
12. MODEL ROUTER
==================================================

Do not hard-code the application around a single AI model.

Create a model-router abstraction.

Possible routing categories:

- Fast model
- Reasoning model
- Coding model
- Vision model
- Local model
- Private/offline model
- Fallback model

The router should consider, where supported:

- Task complexity
- Required capabilities
- Context requirements
- Vision requirements
- Coding requirements
- Privacy requirements
- Availability
- Latency
- Cost
- Offline status

If a model fails, support appropriate fallback behavior.

Do not fabricate model availability.

==================================================
13. MEMORY SYSTEM
==================================================

NEXUS must have controlled memory.

Memory categories:

- Conversations
- Preferences
- Projects
- Tasks
- Goals
- Important information
- Approved files/documents
- Devices
- Useful context

Support:

- Store memory
- Search memory
- Retrieve memory
- Update memory
- Delete memory
- Categorize memory
- Tag memory
- Deduplicate memory
- Summarize memory
- Forget topic
- Forget project
- Forget conversation
- Bulk deletion
- Memory export
- Memory reset
- Memory audit
- Memory privacy controls

Example:

"Forget everything about Project X."

NEXUS must actually remove the applicable stored memory rather than merely saying that it forgot.

Do not store sensitive information unnecessarily.

==================================================
14. COMPUTER STATE AWARENESS
==================================================

NEXUS should be able to understand the current authorized environment:

- Active application
- Active window
- Open windows
- Running processes
- CPU
- RAM
- GPU
- Disk
- Network
- Battery
- Displays
- Terminal sessions
- Browser tabs
- Workspace state
- Connected devices

The system-status panel must reflect actual available information.

==================================================
15. APPLICATION CONTROL
==================================================

Support authorized:

- Application discovery
- Application search
- Application launch
- Application close
- Application restart
- Application focus
- Application status
- Application window discovery
- Application crash detection
- Application freeze detection
- Application recovery
- Application configuration inspection
- Application logs
- Application diagnostics

Never display a successful action unless the underlying operation actually succeeded.

==================================================
16. WINDOW & WORKSPACE CONTROL
==================================================

Support:

- Window listing
- Window search
- Window focus
- Minimize
- Maximize
- Restore
- Move
- Resize
- Fullscreen
- Snap
- Multi-monitor movement
- Virtual desktop discovery
- Virtual desktop switching
- Workspace creation
- Workspace state
- Workspace layout saving
- Workspace restoration
- Window arrangement

Preserve user state where possible.

==================================================
17. TERMINAL CONTROL
==================================================

Provide a controlled terminal subsystem supporting:

- Terminal discovery
- Terminal session creation
- Terminal session management
- Working-directory management
- Approved command execution
- Output reading
- Exit-status detection
- Timeout detection
- Cancellation
- Retry
- Command explanation
- Command-risk assessment
- Command logging

Before consequential commands, use the permission system.

Never hide terminal actions from the user.

==================================================
18. FILE SYSTEM
==================================================

Support authorized:

- File search
- File read
- File creation
- File editing
- File writing
- File rename
- File copy
- File move
- File deletion
- File restoration
- Folder creation
- Folder management
- Metadata inspection
- Content search
- Duplicate detection
- File-version handling
- Storage analysis
- Recovery

Destructive operations must respect the permission policy.

Where technically possible, provide reversible operations and recovery.

==================================================
19. BROWSER CONTROL
==================================================

Support:

- Browser launch
- Tabs
- New tabs
- Closing tabs
- Switching tabs
- Navigation
- Back
- Forward
- Refresh
- Page reading
- Search
- Page extraction
- Text extraction
- Screenshot
- UI inspection
- Form inspection
- Approved typing
- Approved clicking
- Scrolling
- Downloads
- Browser history
- Bookmarks
- Page-change monitoring
- Web research

Actions with external consequences must respect confirmation policies.

==================================================
20. LIVE NEWS / HEADLINES
==================================================

Include a dedicated live intelligence/news section.

Display:

- Headlines
- Technology
- AI
- Science
- World
- Business
- Space
- User-selected topics

Support frequent background refresh where technically possible, approximately every minute when the live-update mode is enabled.

Do not pretend data is live if the underlying source cannot provide current information.

Avoid unnecessary full-page refreshes.

Show:

- New headline indicator
- Updated timestamp
- Source
- Topic
- Summary
- Read/open action

Deduplicate repeated stories where possible.

==================================================
21. CODING SYSTEM
==================================================

NEXUS should function as a coding computer assistant.

Support:

- Project discovery
- Project creation
- Project opening
- Project inspection
- Project structure analysis
- Code search
- Code reading
- Code generation
- Code editing
- Code explanation
- Code review
- Refactoring
- Formatting
- Linting
- Debugging
- Dependency inspection
- Dependency management
- Build
- Run
- Stop
- Development server
- Tests
- Test analysis
- Logs
- Error diagnosis
- Git inspection
- Branch inspection
- Diff inspection
- Commit assistance
- Configuration inspection
- Environment validation
- Architecture analysis
- Documentation generation

Do not silently make destructive project changes.

==================================================
22. TASKS / PROJECTS / GOALS
==================================================

Support:

- Tasks
- Projects
- Goals
- Deadlines
- Priorities
- Dependencies
- Progress
- History
- Search
- Filtering
- Sorting
- Archiving
- Restoration

A complex task should be able to become a structured execution plan.

==================================================
23. AUTOMATION
==================================================

Support:

- Reminders
- Recurring reminders
- Scheduled tasks
- Automations
- Automation history
- Automation status
- Automation failure detection
- Automation recovery
- Workflow creation
- Workflow execution
- Workflow pause
- Workflow resume
- Workflow cancellation

Example:

"Every evening at 7, remind me to study."

Create a real persistent automation if the platform supports it.

Never display an automation as active unless it actually exists and is enabled.

==================================================
24. MULTI-DEVICE SYSTEM
==================================================

Design NEXUS for future multi-device operation.

Potential devices:

- Desktop
- Laptop
- Phone
- Tablet
- ESP32
- IoT devices

Support architecture for:

- Device discovery
- Device registration
- Device status
- Device capabilities
- Device pairing
- Device grouping
- Device commands
- Device permissions
- Device synchronization
- Device health

Do not invent unsupported hardware integrations.

==================================================
25. NETWORK DIAGNOSTICS
==================================================

Support appropriate network diagnostics:

- Network interfaces
- Wi-Fi status
- Signal information
- Ethernet status
- Connectivity
- DNS
- Gateway
- Latency
- Packet loss
- Bandwidth measurement
- Active connections
- Network usage
- Network errors
- VPN status
- Proxy status
- Firewall status
- Local network discovery

Present diagnostics in understandable language.

==================================================
26. STORAGE & BACKUP
==================================================

Support:

- Disk discovery
- Partition information
- Volume information
- Free-space monitoring
- Storage usage analysis
- Large-file detection
- Duplicate analysis
- Temporary-file analysis
- Cache analysis
- Backup discovery
- Backup status
- Backup creation
- Backup verification
- Backup restoration
- Snapshot management where supported
- Recovery-point inspection

Never delete storage data merely because it appears unnecessary without appropriate authorization.

==================================================
27. SECURITY / PERMISSION ENGINE
==================================================

NEXUS must NOT have unrestricted invisible authority.

Create a centralized permission engine.

Permission categories:

READ
CREATE
MODIFY
EXECUTE
DELETE
EXTERNAL ACTION
SYSTEM-CRITICAL

Risk levels:

LOW
MEDIUM
HIGH
CRITICAL

The user must be able to configure permissions.

For consequential actions, request confirmation when required.

Examples:

Low-risk:
- Read system status
- Analyze a provided screenshot
- Search approved memory

Potentially moderate:
- Create files
- Modify projects
- Open applications

High-impact:
- Delete files
- Execute consequential system commands
- Modify critical configuration
- Perform external consequential actions

The permission engine must be centralized rather than implemented inconsistently across individual tools.

==================================================
28. EMERGENCY STOP
==================================================

Provide a highly visible global Emergency Stop.

When activated:

- Stop active agent execution
- Stop queued computer actions
- Stop mouse actions
- Stop keyboard actions
- Stop browser automation
- Stop terminal execution where safely cancellable
- Stop active automation workflows where supported
- Mark the task interrupted
- Preserve relevant state
- Clearly report what stopped

The emergency stop must remain accessible while autonomous execution is active.

==================================================
29. TOOL REGISTRY
==================================================

Create an expandable tool registry.

The initial architecture must support at least 1,500 tools/capabilities across:

- Voice
- Audio
- Vision
- AI reasoning
- Model routing
- Memory
- Knowledge
- Files
- Documents
- Computer control
- Mouse
- Keyboard
- Displays
- Windows
- Applications
- Processes
- Services
- Terminal
- Browser
- Coding
- Tasks
- Projects
- Goals
- Automation
- Devices
- IoT
- Network
- Storage
- Backup
- Security
- Diagnostics

Do not expose 1,500 buttons.

Tools are selected automatically by the cognitive system.

==================================================
30. TOOL CONTRACT
==================================================

Every tool should have a structured definition containing:

- Unique ID
- Name
- Description
- Category
- Input schema
- Output schema
- Permission requirement
- Risk level
- Required context
- Availability
- Timeout
- Cancellation behavior
- Retry policy
- Error behavior
- Verification strategy
- Reversibility
- Audit event

A tool must report real execution status.

Possible states:

- Available
- Unavailable
- Waiting
- Awaiting permission
- Queued
- Running
- Verifying
- Completed
- Failed
- Cancelled
- Interrupted

==================================================
31. ACTION VERIFICATION
==================================================

Every computer-control action that matters must have a verification strategy.

Example:

OPEN APPLICATION
→ send launch request
→ observe process/window state
→ verify application opened
→ continue

CLICK BUTTON
→ identify target
→ click
→ inspect screen/UI state
→ verify expected change
→ continue or recover

TYPE TEXT
→ identify input
→ type
→ verify expected content
→ continue

If verification fails:

- Re-observe
- Diagnose
- Retry if safe
- Re-plan if necessary
- Ask the user if required

Never claim success without evidence.

==================================================
32. FAILURE RECOVERY
==================================================

Handle:

- Network failure
- Model failure
- Tool failure
- Permission denial
- Missing file
- Application not found
- Application crash
- Frozen application
- Unexpected screen
- Changed UI
- Invalid input
- Duplicate operation
- Timeout
- Interrupted task
- Storage failure
- Synchronization conflict
- Offline state

Recovery flow:

DETECT
→ EXPLAIN
→ RECOVER IF SAFE
→ VERIFY
→ CONTINUE OR ASK USER

==================================================
33. INTERRUPTION
==================================================

The user must be able to interrupt NEXUS at any time.

Support:

- Voice interruption
- Text interruption
- Cancel current task
- Pause current task
- Resume task
- Emergency stop

Preserve task state when appropriate.

==================================================
34. OFFLINE MODE
==================================================

Where technically possible, support local/offline functionality.

Offline behavior should include:

- Detect offline state
- Select local model if available
- Continue local capabilities
- Queue eligible operations
- Clearly identify unavailable online functionality
- Synchronize later
- Resolve conflicts

Never pretend online operations succeeded while offline.

==================================================
35. MEMORY + PRIVACY
==================================================

Give users clear controls over:

- What is remembered
- What is not remembered
- Memory deletion
- Project forgetting
- Conversation deletion
- Data export
- Data reset
- Permission history
- Tool activity
- Device access

Camera and screen access must have clear active-state indicators.

==================================================
36. MAIN UI
==================================================

The UI must NOT look like a generic chatbot.

Use a premium futuristic sci-fi/JARVIS-inspired command-center aesthetic.

Primary structure:

LEFT:
- NEXUS
- Memory
- Tasks
- Projects
- Automations
- Devices
- Tools
- History

CENTER:
- Voice Sphere
- Conversation
- Current task
- AI state
- Visual context
- Execution progress

RIGHT:
- System Status
- CPU
- RAM
- GPU
- Network
- Storage
- Active applications
- Device status
- AI activity

BOTTOM:
- Voice/text input
- Microphone
- Camera/vision
- Screen context
- Send
- Interrupt
- Emergency Stop when applicable

The interface must remain clean.

Do not turn it into a dashboard full of meaningless widgets.

==================================================
37. VOICE SPHERE STATES
==================================================

The central sphere must clearly communicate:

IDLE
LISTENING
UNDERSTANDING
THINKING
PLANNING
WAITING FOR PERMISSION
EXECUTING
VERIFYING
SPEAKING
INTERRUPTED
PAUSED
ERROR
OFFLINE
COMPLETED

Animations should be subtle, performant and meaningful.

Do not use excessive animations.

==================================================
38. LIVE AI ACTIVITY
==================================================

During complex execution, show a concise activity stream.

Example:

NEXUS
Preparing coding environment

✓ Project found
✓ VS Code opened
✓ Workspace detected
● Starting development server
○ Verification pending

Do not expose unnecessary internal reasoning or hidden chain-of-thought.

Show actionable execution status, not private reasoning.

==================================================
39. COMPLETE SCREEN INVENTORY
==================================================

Build and fully define at minimum:

1. Launch/Splash
2. Main Command Center
3. Voice Interaction
4. Conversation
5. Vision Mode
6. Screen Analysis
7. Camera Mode
8. Agent/Task Execution
9. Task Details
10. Projects
11. Project Details
12. Memory
13. Memory Details
14. Live Headlines
15. News Details
16. Browser Control
17. Computer Activity
18. Applications
19. Windows/Workspaces
20. Terminal
21. Files
22. File Details
23. Coding Workspace
24. System Monitor
25. Network Diagnostics
26. Storage
27. Devices
28. Automations
29. Automation Details
30. Tool Registry
31. Permissions
32. Security/Privacy
33. Settings
34. AI/Model Settings
35. Voice Settings
36. Appearance
37. History/Audit
38. Notifications
39. Error/Recovery state
40. Offline state
41. Emergency Stop state

For EVERY screen define:

- Purpose
- Entry conditions
- Exit conditions
- Header
- Navigation
- Content
- Primary action
- Secondary actions
- Controls
- Input behavior
- Scroll behavior
- Loading state
- Empty state
- Error state
- Success state
- Disabled state
- Permission state
- Back behavior
- Dismiss behavior
- Result of each action

Do not leave mandatory behavior unspecified.

==================================================
40. NAVIGATION
==================================================

Navigation must feel native and predictable.

Define:

- Root navigation
- Secondary navigation
- Back behavior
- Modal behavior
- Sheets/dialogs
- Deep navigation
- State preservation
- Return destinations
- Interrupted-task restoration

Do not unexpectedly reset user context when navigating.

==================================================
41. RESPONSIVE EXPERIENCE
==================================================

Support:

- Desktop
- Laptop
- Tablet
- Mobile where applicable
- Portrait
- Landscape
- Multiple screen sizes
- Multiple monitors
- Keyboard/mouse
- Touch
- Safe areas
- Notches
- Viewport changes
- Virtual keyboard
- Large text
- Accessibility scaling

Mobile must feel like a real native-quality application.

Do not simply squeeze a desktop dashboard onto a phone.

==================================================
42. ACCESSIBILITY
==================================================

Support:

- Semantic controls
- Keyboard navigation
- Visible focus
- Logical focus order
- Screen-reader labels
- Accessible forms
- Accessible dialogs
- Accessible error messages
- Adequate contrast
- Font scaling
- Reduced motion
- Non-color-only status indicators
- Touch accessibility

==================================================
43. VISUAL DESIGN
==================================================

Visual direction:

- Futuristic
- Premium
- Technical
- Intelligent
- Minimal
- Dark command-center aesthetic where appropriate
- Strong visual hierarchy
- Refined typography
- High-quality spacing
- Clear surfaces
- Subtle glow/depth
- Professional data visualization
- Controlled sci-fi styling

Avoid:

- Cheap neon overload
- Excessive glass effects
- Random gradients
- Generic AI-chat UI
- Excessive rounded cards
- Unnecessary animations
- Clutter

The design should communicate intelligence and control.

==================================================
44. INTERACTION QUALITY
==================================================

Every important control needs:

- Normal
- Hover
- Focus
- Pressed
- Disabled
- Loading
- Success
- Error

states where applicable.

Every action should provide appropriate feedback.

Avoid animations that slow down actual work.

Support reduced-motion behavior.

==================================================
45. PERSISTENCE
==================================================

Persist appropriate:

- User settings
- Preferences
- Memory
- Tasks
- Projects
- Automations
- Device configuration
- Tool permissions
- UI preferences
- Relevant activity history

Clearly separate:

PERSISTENT STATE
TEMPORARY STATE
SESSION STATE
TASK EXECUTION STATE

Do not lose important state after navigation or application restart.

==================================================
46. DATA FLOW
==================================================

Maintain clear separation between:

- UI
- State
- Cognitive engine
- Memory
- Tool registry
- Permission engine
- Computer-control layer
- Model router
- Persistence
- Device integrations
- Diagnostics

Do not tightly couple unrelated functionality.

==================================================
47. PERFORMANCE
==================================================

Prioritize:

- Fast startup
- Efficient rendering
- Low unnecessary CPU usage
- Low memory usage
- Efficient screen capture
- Efficient visual analysis
- Efficient event handling
- Tool execution isolation
- Background-task efficiency
- Incremental updates
- Avoiding unnecessary full-screen re-renders
- Graceful degradation on weaker hardware

The application must remain responsive while NEXUS is performing background work.

==================================================
48. RELIABILITY
==================================================

Mandatory reliability requirements:

- No fake success
- No fake tool execution
- No fake progress
- No dead controls
- No mandatory TODOs
- No unfinished mandatory features
- No placeholder functionality
- No "coming soon" for required functionality
- No silent failures
- No swallowed errors
- No uncontrolled loops
- No infinite autonomous execution
- Clear cancellation
- Clear recovery
- Clear task state

==================================================
49. TOOL EXECUTION SAFETY
==================================================

Autonomous execution must have:

- Maximum execution boundaries
- Timeout handling
- Cancellation
- Permission checks
- Risk classification
- Verification
- Failure recovery
- Audit events
- Emergency stop

Do not create an autonomous loop that can continue indefinitely without a controlled stopping condition.

==================================================
50. AUDIT / ACTIVITY
==================================================

Maintain a user-visible activity history for meaningful operations.

Include:

- Timestamp
- Action
- Tool
- Target
- Result
- Status
- Permission state
- Error if applicable

Do not expose hidden model chain-of-thought.

Show concise operational information instead.

==================================================
51. ERROR UX
==================================================

Errors should explain:

WHAT happened
WHY it happened when known
WHAT NEXUS tried
WHAT can happen next

Example:

"VS Code could not be opened because the application was unavailable."

Offer appropriate actions:

- Retry
- Choose another application
- Cancel
- Diagnose

Do not show meaningless technical errors as the only explanation.

==================================================
52. SETTINGS
==================================================

Include settings for:

- Voice
- Wake word
- Response mode
- Models
- Model routing
- Memory
- Permissions
- Tool authorization
- Computer control
- Browser control
- Camera
- Screen access
- Notifications
- Automation
- Privacy
- Appearance
- Accessibility
- Performance
- Devices
- Network
- Activity history

==================================================
53. NO AGENT TOWN
==================================================

Do NOT include an "Agent Town" interface.

Agent functionality must be represented through:

- Current task
- Execution timeline
- Tool activity
- Progress
- Status
- Result
- Verification

Keep the experience focused and professional.

==================================================
54. NO GENERIC CHATBOT DESIGN
==================================================

Do not make the central experience a standard chat page with a sidebar and message bubbles as the primary interface.

Conversation is supported, but NEXUS is fundamentally a computer/AI command system.

The Voice Sphere and live system context should be central to the experience.

==================================================
55. SECURITY AND PRIVACY
==================================================

Apply least privilege where practical.

Do not silently access:

- Camera
- Microphone
- Files
- Screen
- Devices
- Browser
- System controls

without appropriate authorization.

Clearly communicate active access.

Provide permission revocation.

Provide emergency stop.

Provide data deletion controls.

==================================================
56. FUTURE EXTENSIBILITY
==================================================

The architecture must allow additional tools to be added without rewriting the entire system.

New capabilities should register through the tool system.

Future possibilities include:

- More AI providers
- More local models
- More devices
- More IoT integrations
- More applications
- More automation providers
- More operating systems
- More browser capabilities

Do not hard-code the architecture around the current 1,500 tools.

==================================================
57. ENGINEERING RULES
==================================================

Unless explicitly overridden by the environment:

- Keep architecture modular
- Avoid unnecessary dependencies
- Avoid unnecessary frameworks
- Do not add unrelated services
- Do not invent external integrations
- Do not invent APIs
- Do not fabricate credentials
- Do not fake unavailable capabilities
- Keep state ownership clear
- Keep tool execution isolated
- Validate inputs
- Validate outputs
- Handle errors
- Handle cancellation
- Handle interruptions
- Preserve state
- Make mandatory features functional

If a required capability depends on a platform API, implement the correct integration abstraction rather than pretending the capability exists.

==================================================
58. PLATFORM BEHAVIOR
==================================================

The computer-control implementation must respect the actual operating system and application environment.

Do not assume every application exposes automation APIs.

When direct application APIs are unavailable, the architecture may use supported computer-vision/input mechanisms where technically and legitimately available.

Always distinguish:

- Native API control
- Browser automation
- Accessibility/UI automation
- Keyboard/mouse control
- Visual computer interaction
- Unsupported operation

Never pretend an unsupported mechanism is available.

==================================================
59. AI RESPONSE BEHAVIOR
==================================================

NEXUS should communicate concisely during execution.

Instead of verbose explanations:

"Understood. I'm checking your active applications."

Then execute.

For long tasks:

"Working on it."

Show progress through the activity interface.

When complete:

"Done. Your development environment is ready."

If something fails:

"I couldn't complete the final step because the development server failed to start. I found the error and can continue troubleshooting."

==================================================
60. EXAMPLE AUTONOMOUS WORKFLOW
==================================================

User:

"Hey Nexus, why is my computer slow?"

NEXUS:

1. Detect intent.
2. Inspect system state.
3. Check CPU.
4. Check RAM.
5. Check GPU if relevant.
6. Check disk usage.
7. Inspect resource-heavy processes.
8. Check storage pressure.
9. Check network if relevant.
10. Compare findings.
11. Determine likely causes.
12. Verify suspicious processes/services.
13. Present findings.
14. Ask before consequential remediation.
15. Apply authorized remediation.
16. Verify system state.
17. Report results.

Do not blindly kill processes or modify the system simply because resource usage is high.

==================================================
61. EXAMPLE VISUAL WORKFLOW
==================================================

User:

"Nexus, what's wrong with this screen?"

NEXUS:

1. Obtain authorized screen context.
2. Capture relevant screen state.
3. Detect application/UI.
4. Read visible error.
5. Understand context.
6. Analyze likely cause.
7. Provide explanation.
8. Offer possible actions.
9. If authorized, perform safe remediation.
10. Verify the result.

==================================================
62. EXAMPLE CODING WORKFLOW
==================================================

User:

"Nexus, find why my project isn't running."

NEXUS:

1. Identify project.
2. Inspect project structure.
3. Inspect runtime configuration.
4. Inspect dependencies.
5. Inspect recent errors/logs.
6. Run appropriate diagnostic operation.
7. Analyze failure.
8. Propose or perform authorized correction.
9. Run again.
10. Verify.
11. Report exact outcome.

==================================================
63. EXAMPLE MULTI-APPLICATION WORKFLOW
==================================================

User:

"Open my project, start the development environment and show me the result."

NEXUS should:

- Find project
- Open appropriate application
- Open project
- Open terminal if needed
- Start approved development process
- Monitor output
- Detect errors
- Use screen/application state
- Verify result
- Report completion

==================================================
64. MOBILE COMPANION
==================================================

Where supported, the mobile interface should act as a companion/control surface.

It may show:

- NEXUS status
- Voice Sphere
- Current task
- Agent progress
- Notifications
- Device status
- Emergency Stop
- Approval requests
- Activity

Do not assume the mobile device can directly perform computer operations without a real connection.

==================================================
65. APPROVAL EXPERIENCE
==================================================

When approval is required, present:

ACTION
TARGET
REASON
RISK
WHAT WILL CHANGE

Then:

ALLOW
DENY
CANCEL

Do not disguise approval requests.

==================================================
66. COMPLETION EXPERIENCE
==================================================

Every major task should end in one of:

COMPLETED
PARTIALLY COMPLETED
FAILED
CANCELLED
INTERRUPTED
WAITING FOR USER

Provide the relevant explanation.

==================================================
67. QUALITY ASSURANCE
==================================================

Before considering the application complete, verify:

FUNCTIONALITY
- Voice
- Text
- Vision
- Memory
- Model routing
- Tools
- Computer control
- Mouse
- Keyboard
- Applications
- Windows
- Terminal
- Files
- Browser
- Coding
- Tasks
- Automation
- Devices
- Network
- Storage

NAVIGATION
- All screens
- Back behavior
- Modal behavior
- State preservation
- Deep navigation

STATE
- Loading
- Empty
- Error
- Success
- Disabled
- Permission
- Offline
- Interrupted
- Cancelled

COMPUTER CONTROL
- Mouse
- Keyboard
- Visual targeting
- Action verification
- Cancellation
- Recovery

AGENT
- Planning
- Execution
- Replanning
- Verification
- Failure recovery
- Interruption
- Resume

PERSISTENCE
- Settings
- Memory
- Tasks
- Projects
- Automations
- Permissions
- Relevant history

ACCESSIBILITY
- Keyboard
- Screen reader
- Focus
- Contrast
- Font scaling
- Reduced motion

PERFORMANCE
- Startup
- Rendering
- Memory usage
- CPU usage
- Background activity

PRIVACY
- Microphone
- Camera
- Screen
- Files
- Device access
- Memory

==================================================
68. PLACEHOLDER PROHIBITION
==================================================

Never use:

- Fake buttons
- Fake agent execution
- Fake computer control
- Fake live news
- Fake system statistics
- Fake memory
- Fake automation
- Fake progress
- Static screens pretending to be functional
- Lorem ipsum
- "Coming soon"
- Unwired mandatory controls
- Unimplemented mandatory features

If something cannot actually be implemented in the target environment, clearly isolate it as unavailable rather than pretending it works.

==================================================
69. REQUIREMENT PRIORITY
==================================================

Follow explicit requirements first.

Do not silently add unrelated major functionality.

Do not remove specified functionality.

Resolve minor unspecified details using the most consistent:

- Usable
- Accessible
- Maintainable
- Performant
- Native-feeling

choice.

==================================================
70. FINAL PRODUCT EXPERIENCE
==================================================

The finished product should feel like:

A personal AI operating layer.

Not:

A chatbot with a fancy background.

The user should feel that NEXUS understands:

- Them
- Their goals
- Their computer
- Their screen
- Their applications
- Their projects
- Their files
- Their tasks
- Their devices
- Their context

while remaining transparent about what it can actually access and do.

The experience should be:

FAST
INTELLIGENT
NATURAL
RESPONSIVE
TRUSTWORTHY
VISUAL
VOICE-FIRST
COMPUTER-AWARE
GOAL-ORIENTED
EXTENSIBLE

==================================================
71. FINAL NON-NEGOTIABLE REQUIREMENTS
==================================================

Build the complete application.

Do NOT make a demo.
Do NOT make a prototype.
Do NOT generate fake functionality.
Do NOT leave mandatory features unfinished.
Do NOT add unrelated features.
Do NOT ignore specified states.
Do NOT ignore specified flows.
Do NOT ignore edge cases.
Do NOT claim actions succeeded without verification.
Do NOT expose hidden chain-of-thought.
Do NOT create uncontrolled autonomous execution.
Do NOT remove the user's ability to stop or authorize consequential actions.
Do NOT make NEXUS a generic chatbot.
Do NOT include Agent Town.

Implement the complete NEXUS/JARVIS experience with:

AGI-STYLE COGNITION
VOICE
VOICE SPHERE
VISION
SCREEN UNDERSTANDING
MEMORY
MODEL ROUTING
GOAL MANAGEMENT
AUTONOMOUS TASK EXECUTION
MOUSE CONTROL
KEYBOARD CONTROL
WINDOW CONTROL
APPLICATION CONTROL
TERMINAL CONTROL
FILE CONTROL
BROWSER CONTROL
CODING CONTROL
SYSTEM MONITORING
NETWORK DIAGNOSTICS
STORAGE MANAGEMENT
DEVICE CONTROL
AUTOMATION
LIVE INTELLIGENCE
PERMISSIONS
VERIFICATION
RECOVERY
AUDIT
PRIVACY
ACCESSIBILITY
PERFORMANCE
RESPONSIVENESS

Every mandatory feature must have real behavior.

Generate fully functional app.