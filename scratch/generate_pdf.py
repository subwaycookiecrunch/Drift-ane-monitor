import os
import sys
import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

def draw_header_footer(canvas, doc):
    canvas.saveState()
    # Header
    canvas.setFont('Helvetica-Bold', 8)
    canvas.setFillColor(colors.HexColor('#5DC9A5'))
    canvas.drawString(54, 750, "drift — APPLE NEURAL ENGINE MONITOR")
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(colors.HexColor('#888888'))
    canvas.drawRightString(doc.pagesize[0]-54, 750, "TECHNICAL DESIGN & ARCHITECTURE")
    canvas.setStrokeColor(colors.HexColor('#e0e0e0'))
    canvas.setLineWidth(0.5)
    canvas.line(54, 742, doc.pagesize[0]-54, 742)
    
    # Footer
    canvas.line(54, 60, doc.pagesize[0]-54, 60)
    canvas.setFont('Helvetica', 8)
    canvas.drawString(54, 45, f"Generated on {datetime.datetime.now().strftime('%Y-%m-%d')}")
    canvas.drawRightString(doc.pagesize[0]-54, 45, f"Page {doc.page}")
    canvas.restoreState()

def build_pdf(filename="drift_design_document.pdf"):
    # Target 0.75 in margins
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=72,
        bottomMargin=72
    )
    
    styles = getSampleStyleSheet()
    
    # Custom Styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=15
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#555555'),
        spaceAfter=30
    )
    
    h1_style = ParagraphStyle(
        'SectionH1',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor('#222222'),
        spaceBefore=15,
        spaceAfter=10,
        keepWithNext=True
    )

    h2_style = ParagraphStyle(
        'SectionH2',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        textColor=colors.HexColor('#333333'),
        spaceBefore=10,
        spaceAfter=6,
        keepWithNext=True
    )
    
    body_style = ParagraphStyle(
        'BodyTextCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor('#333333'),
        spaceAfter=10
    )
    
    bullet_style = ParagraphStyle(
        'BulletCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor('#333333'),
        leftIndent=15,
        firstLineIndent=-10,
        spaceAfter=5
    )

    code_style = ParagraphStyle(
        'CodeBlock',
        parent=styles['Normal'],
        fontName='Courier',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#111111'),
    )
    
    math_style = ParagraphStyle(
        'MathDisplay',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#2a2a2a'),
        leftIndent=25,
        spaceBefore=6,
        spaceAfter=6
    )

    story = []
    
    # ------------------ COVER PAGE / TITLE ------------------
    story.append(Spacer(1, 40))
    story.append(Paragraph("drift — Apple Neural Engine Monitor", title_style))
    story.append(Paragraph("Technical Design, Architecture, and Codebase Specification Document", subtitle_style))
    story.append(Spacer(1, 20))
    
    # Divider line
    divider = Table([['']], colWidths=[doc.width])
    divider.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,-1), 1.5, colors.HexColor('#5DC9A5')),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(divider)
    story.append(Spacer(1, 25))
    
    # ------------------ SECTION 1 ------------------
    story.append(Paragraph("1. Core Philosophy & Architectural Design", h1_style))
    story.append(Paragraph(
        "drift is a production-grade, zero-dependency, zero-sudo Apple Neural Engine (ANE) monitor and benchmark suite "
        "designed for Apple Silicon Macs. Historically, monitoring Apple Neural Engine utilization and memory residency "
        "demanded running macOS private powermetrics tools, requiring elevated root privileges (sudo) and consuming substantial "
        "CPU resources.", body_style))
    story.append(Paragraph(
        "drift breaks this barrier by query-routing directly into XNU kernel process diagnostics and private hardware clients "
        "from standard user space. By mapping core telemetry to Mach ports and structures through ctypes bindings, the monitor "
        "requires no compilation entitlements or root bypasses. It implements the following key structural rules:", body_style))
    
    story.append(Paragraph("• <b>Zero-Sudo Per-Process Attribution:</b> Queries general process state mapping through kernel rusage logs to isolate Neural Core active states.", bullet_style))
    story.append(Paragraph("• <b>Minimal Polling Overhead:</b> Integrates a 2-second Time-To-Live (TTL) cache for hot path scans (such as active file descriptor checks), reducing resource usage to below 1.5% CPU.", bullet_style))
    story.append(Paragraph("• <b>Decoupled Async Disk I/O:</b> Isolates database write buffers onto a dedicated queue writer running Write-Ahead Logging (WAL) transaction commits. This prevents disk blocking latency from impacting TUI UI rendering.", bullet_style))
    
    story.append(Spacer(1, 15))
    
    # ------------------ SECTION 2 ------------------
    story.append(Paragraph("2. File-by-File Codebase Walkthrough", h1_style))
    story.append(Paragraph(
        "The application divides tasks into dedicated modules to separate ctypes binding orchestration, TUI screens, "
        "benchmark tests, and stress-harness compilers.", body_style))
    
    story.append(Paragraph("<b>drift.py: Main TUI and CLI Orchestration</b>", h2_style))
    story.append(Paragraph(
        "This file manages CLI parsing, setup configurations, and launches the Textual application framework. "
        "It defines packed C-structures for rusage mappings and SMC client buffers, creates the primary DataCollector loop, "
        "maintains event logging, and hosts the TUI Screen stacks: FingerprintScreen (active models / event timelines), "
        "MainScreen (detailed htop list), LeaderboardScreen (cumulative energy ∑ mJ), CompareScreen (side-by-side vertical panels), "
        "and WatchScreen (focused process overlays).", body_style))
        
    story.append(Paragraph("<b>model_detect.py: Active Model Scanner</b>", h2_style))
    story.append(Paragraph(
        "Loads libproc.dylib and binds proc_pidinfo flavors (PROC_PIDLISTFDS, PROC_PIDFDVNODEPATHINFO) via ctypes. "
        "It maps open file descriptor integers to absolute vnode file paths. These paths are verified against regular "
        "expressions for frameworks: .safetensors (MLX), .mlmodelc (CoreML), and .gguf (Ollama / llama.cpp). "
        "Employs a thread-safe 2.0-second TTL cache.", body_style))

    story.append(Paragraph("<b>bench.py: Benchmark Coordinator</b>", h2_style))
    story.append(Paragraph(
        "Coordinates benchmark execution: compiles the Swift workload, starts the target subprocess, polls active power profiles, "
        "computes IPC, tracks temperatures, and calculates TOPS ratings based on Apple Silicon generation budgets.", body_style))

    story.append(Paragraph("<b>hammer_ane.swift: Sustained ANE Workload</b>", h2_style))
    story.append(Paragraph(
        "A native Swift program designed to fully load the Neural Engine. By creating concurrent threads running Vision OCR "
        "text recognition requests in tight loops, it stresses ANE circuits, forcing the system to record maximum "
        "current and power draws.", body_style))

    story.append(Paragraph("<b>drift.tcss: UI Layout Stylesheet</b>", h2_style))
    story.append(Paragraph(
        "Vanilla CSS rules configured for Textual. It controls layout grids, flex containers, scrolling logs, and color styling "
        "(utilizing high-contrast dark tones and bright green/cyan highlights to emulate retro-native terminals).", body_style))

    story.append(Spacer(1, 15))

    # ------------------ SECTION 3 ------------------
    story.append(Paragraph("3. Deep Dive into Low-Level Telemetry Integrations", h1_style))
    
    story.append(Paragraph("<b>Per-Process ANE Attribution (rusage_info_v6)</b>", h2_style))
    story.append(Paragraph(
        "drift calls proc_pid_rusage to fetch resource utilization statistics. It parses fields inside the rusage_info_v6 structure:", body_style))
    
    code_text_1 = (
        "class rusage_info_v6(ctypes.Structure):\n"
        "    _fields_ = [\n"
        "        # ...\n"
        "        (\"ri_neural_footprint\", ctypes.c_uint64),\n"
        "        (\"ri_energy_nj\", ctypes.c_uint64),\n"
        "        # ...\n"
        "    ]"
    )
    
    code_table_1 = Table([[Paragraph(code_text_1.replace('\n', '<br/>').replace(' ', '&nbsp;'), code_style)]], colWidths=[doc.width])
    code_table_1.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f5f5f5')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#e0e0e0')),
        ('PADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(code_table_1)
    story.append(Spacer(1, 10))

    story.append(Paragraph("<b>Delta-Based Power Calculations</b>", h2_style))
    story.append(Paragraph(
        "Because the energy returned by ri_energy_nj is a monotonically increasing odometer, the system computes delta metrics "
        "across sampling intervals to derive instantaneous power in milliwatts (mW):", body_style))
    
    story.append(Paragraph("Power (mW) = [ΔEnergy (nanojoules) / 10^6] / Δt (seconds)", math_style))
    story.append(Paragraph(
        "Here, Δt corresponds to the measured time difference between consecutive samples (using time.time()), ensuring "
        "power remains mathematically accurate regardless of thread execution jitter.", body_style))

    story.append(Paragraph("<b>Zero-Sudo SMC Temperature Reader</b>", h2_style))
    story.append(Paragraph(
        "drift establishes a connection to the AppleSMC service client via IOKit and queries registers using IOConnectCallStructMethod. "
        "To support all Apple Silicon generations (M1–M5), the client loops through a fallback check chain:", body_style))
    
    story.append(Paragraph("TC0P (CPU Proximity) → TC0D → TCMb (Mainboard) → TCHP → TH0T → Tp00 → Tp01 → Ts0P", math_style))
    story.append(Paragraph(
        "The system registers the first active sensor key, reads the raw hex bytes, and unpacks the values (either as float "
        "or fixed-point sp78 formats).", body_style))

    story.append(Spacer(1, 15))

    # ------------------ SECTION 4 ------------------
    story.append(Paragraph("4. Feature and Subcommand Breakdown", h1_style))
    
    story.append(Paragraph("<b>drift ps — One-Shot Snapshot</b>", h2_style))
    story.append(Paragraph(
        "Prints a formatted table of active processes directly to stdout and exits. Automatically matches widths, "
        "supports --json output, --sort [power|energy|footprint], and --watch N loops with clean SIGINT handling.", body_style))

    story.append(Paragraph("<b>drift finger — Inference Dashboard</b>", h2_style))
    story.append(Paragraph(
        "The default startup TUI. The upper panel displays active model file paths, framework tags (MLX, CoreML, Ollama, llama.cpp), "
        "power stats, and mini 20-sample sparklines. When inactive, a pulsing centered '◌ ANE idle' container is rendered. "
        "The lower panel logs inference timeline transitions (start/stop) with total energy consumption deltas.", body_style))

    story.append(Paragraph("<b>Thermal Sparkline Overlay (drift watch --thermal)</b>", h2_style))
    story.append(Paragraph(
        "Displays stacked timelines in Watch mode: ANE utilization % and die temperature (°C). Aligns timelines over a "
        "matching 60s history window. Color codes temperature ranges: Normal (<60°C), Amber (60-80°C), Red (>80°C).", body_style))

    story.append(Paragraph("<b>drift history — Database Logger</b>", h2_style))
    story.append(Paragraph(
        "Logs snapshot metadata to ~/.drift/history.db on a separate thread using WAL transactions. Automatically prunes "
        "database logs older than 30 days. Provides CLI summary cards, recent session lists, search options, and JSON exports.", body_style))

    story.append(Spacer(1, 15))

    # ------------------ SECTION 5 ------------------
    story.append(Paragraph("5. Database Schema Reference", h1_style))
    story.append(Paragraph(
        "The background logger tracks sessions, events, and periodic samples using a SQLite database containing the "
        "following three tables:", body_style))

    schema_text = (
        "1. sessions\n"
        "   - id: INTEGER PRIMARY KEY (Unique Session identifier)\n"
        "   - started_at: REAL (Epoch timestamp when session initialized)\n"
        "   - ended_at: REAL (Epoch timestamp when session completed/closed)\n"
        "   - host: TEXT (Host node name)\n"
        "   - chip: TEXT (Processor generation name)\n\n"
        "2. events\n"
        "   - id: INTEGER PRIMARY KEY\n"
        "   - session_id: INTEGER (Foreign key mapping to sessions.id)\n"
        "   - ts: REAL (Event timestamp)\n"
        "   - pid: INTEGER (Process PID)\n"
        "   - process_name: TEXT (Process name)\n"
        "   - model_name: TEXT (Detected model file name)\n"
        "   - framework: TEXT (Machine learning framework type)\n"
        "   - peak_power_mw: REAL (Maximum recorded milliwatts)\n"
        "   - total_energy_mj: REAL (Total cumulative energy in millijoules)\n"
        "   - event_type: TEXT (Either 'start' or 'stop')\n\n"
        "3. samples\n"
        "   - id: INTEGER PRIMARY KEY\n"
        "   - session_id: INTEGER (Foreign key mapping to sessions.id)\n"
        "   - ts: REAL (Snapshot timestamp)\n"
        "   - ane_util_pct: REAL (Total system ANE utilization percent)\n"
        "   - die_temp_c: REAL (Processor die temperature)"
    )

    schema_table = Table([[Paragraph(schema_text.replace('\n', '<br/>').replace(' ', '&nbsp;'), code_style)]], colWidths=[doc.width])
    schema_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#fdfdfd')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#d0d0d0')),
        ('PADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(schema_table)
    
    # ------------------ SECTION 6 ------------------
    story.append(PageBreak())
    
    story.append(Paragraph("6. Verification & Test Suite Specification (Phase 5)", h1_style))
    story.append(Paragraph(
        "To ensure production-grade reliability, a comprehensive test suite was implemented consisting of "
        "140 unit, integration, performance, and property-based tests. The test suite is fully decoupled "
        "from macOS hardware requirements, allowing portable execution in Linux CI/CD environments. "
        "Code coverage targets a strict 85% fail-under threshold, configured with file and block exclusions "
        "for non-testable components (such as direct Textual TUI widgets and raw compiler invocation blocks).", body_style))
        
    table_hdr_style = ParagraphStyle(
        'TableHdr',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.white
    )
    
    table_data = [
        [
            Paragraph("Test File", table_hdr_style),
            Paragraph("Count", table_hdr_style),
            Paragraph("Coverage", table_hdr_style),
            Paragraph("Scope / Description", table_hdr_style)
        ],
        [
            Paragraph("test_power_math.py", body_style),
            Paragraph("15", body_style),
            Paragraph("100%", body_style),
            Paragraph("Checks odometer overflows, zero time delta safety, and chip TOPS maps.", body_style)
        ],
        [
            Paragraph("test_model_detect.py", body_style),
            Paragraph("15", body_style),
            Paragraph("92%", body_style),
            Paragraph("Tests model framework regexes, TTL cache, and concurrent scan safety.", body_style)
        ],
        [
            Paragraph("test_smc_temperature.py", body_style),
            Paragraph("17", body_style),
            Paragraph("100%", body_style),
            Paragraph("Tests SP78 parsing, candidate fallbacks, and thermal color categories.", body_style)
        ],
        [
            Paragraph("test_rusage_struct.py", body_style),
            Paragraph("9", body_style),
            Paragraph("97%", body_style),
            Paragraph("Verifies rusage_info_v6 structure layout, size (464 bytes), and member offsets.", body_style)
        ],
        [
            Paragraph("test_database.py", body_style),
            Paragraph("15", body_style),
            Paragraph("99%", body_style),
            Paragraph("Verifies WAL mode, 30-day session pruning, and corruption recovery.", body_style)
        ],
        [
            Paragraph("test_cli_subcommands.py", body_style),
            Paragraph("18", body_style),
            Paragraph("90%", body_style),
            Paragraph("Checks command parsing and output format for ps, history, bench subcommands.", body_style)
        ],
        [
            Paragraph("test_bench.py", body_style),
            Paragraph("11", body_style),
            Paragraph("100%", body_style),
            Paragraph("Validates duration limits, peak/average power, and swift compile errors.", body_style)
        ],
        [
            Paragraph("test_tui_screens.py", body_style),
            Paragraph("14", body_style),
            Paragraph("96%", body_style),
            Paragraph("Checks DataTable columns, screen switches, and idle screen rendering.", body_style)
        ],
        [
            Paragraph("test_performance.py", body_style),
            Paragraph("8", body_style),
            Paragraph("97%", body_style),
            Paragraph("Validates RSS memory bounds (<50MB) and asynchronous WAL write latency.", body_style)
        ],
        [
            Paragraph("test_edge_cases.py", body_style),
            Paragraph("13", body_style),
            Paragraph("89%", body_style),
            Paragraph("Tests spaces/Unicode paths, extreme sensor bounds, and disk-full scenarios.", body_style)
        ],
        [
            Paragraph("test_regression.py", body_style),
            Paragraph("5", body_style),
            Paragraph("100%", body_style),
            Paragraph("Tests NTP clock jumps, macOS sleep locks, and infinite loop fallbacks.", body_style)
        ],
        [
            Paragraph("<b>Total Test Suite</b>", body_style),
            Paragraph("<b>140</b>", body_style),
            Paragraph("<b>89.89%</b>", body_style),
            Paragraph("<b>140/140 tests pass successfully.</b>", body_style)
        ]
    ]
    
    perf_table = Table(table_data, colWidths=[130, 40, 60, 274])
    perf_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#5DC9A5')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,0), 6),
        ('TOPPADDING', (0,0), (-1,0), 6),
        ('BACKGROUND', (0,1), (-1,-2), colors.HexColor('#f9f9f9')),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#e8f5e9')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#dddddd')),
        ('PADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(perf_table)
    
    # Build the document
    doc.build(story, onFirstPage=draw_header_footer, onLaterPages=draw_header_footer)

if __name__ == "__main__":
    output_pdf = "drift_design_document.pdf"
    if len(sys.argv) > 1:
        output_pdf = sys.argv[1]
    build_pdf(output_pdf)
    print(f"PDF generated successfully at {output_pdf}")
