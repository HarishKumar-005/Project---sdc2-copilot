"""Seed public.product_master in PostgreSQL with 240 deterministic rows across 8 categories.

Usage:
    python scripts/seed_product_master.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.scd2_copilot.config import get_settings
from src.scd2_copilot.db.connection import DatabaseManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scd2_copilot.seed")

CATEGORIES_DATA: dict[str, dict] = {
    "Electronics": {
        "prefix": "SUP-ELEC",
        "base_price": 149.99,
        "price_step": 25.00,
        "items": [
            "Smart TV 55-inch 4K",
            "Bluetooth Speaker Portable",
            "Noise-Cancelling Headphones Pro",
            "4K Action Camera Waterproof",
            "Wireless Earbuds ANC",
            "Smartwatch Series 5 GPS",
            "Portable Mini Projector HD",
            "Gaming Monitor 27-inch 165Hz",
            "Home Theater Soundbar 2.1",
            "Wireless Fast Charging Pad",
            "Compact Drone 4K Gimbal",
            "Virtual Reality Headset 128GB",
            "Gaming Headset 7.1 Surround",
            "Streaming Media Box 4K",
            "Mirrorless Camera Body 24MP",
            "Prime Camera Lens 50mm f/1.8",
            "Digital Voice Recorder 16GB",
            "Smart Thermostat Wi-Fi",
            "Security Camera Outdoor Pro",
            "Video Doorbell 2K HDR",
            "LED Video Light Studio Panel",
            "Vlogging Compact Camera",
            "Fitness Smart Tracker Band",
            "E-Reader Paperwhite 32GB",
            "Retro Bluetooth FM Radio",
            "Direct-Drive Turntable Hi-Fi",
            "USB Audio Interface 2-In 2-Out",
            "Cardioid Condenser Studio Mic",
            "3-Axis Handheld Phone Gimbal",
            "Wireless HDMI Transmitter Kit",
        ],
    },
    "Computers": {
        "prefix": "SUP-COMP",
        "base_price": 499.00,
        "price_step": 65.00,
        "items": [
            "Workstation Pro Tower 16-Core",
            "Ultrabook Thin 14-inch i7",
            "Gaming Laptop RTX 4080 16GB",
            "Mini PC Desktop Core i7 32GB",
            "All-in-One Desktop 27-inch 4K",
            "Rackmount Enterprise Server 1U",
            "Developer Laptop 15-inch 32GB",
            "Chromebook Enterprise 14-inch",
            "Thin Client Cloud Terminal",
            "Industrial Touch Panel PC 15",
            "Creator Desktop Studio 64GB",
            "Compact Micro Desktop Slim",
            "Rugged Field Laptop Tough",
            "Dual-Screen Folding Laptop",
            "AI Deep Learning Rig 64GB",
            "Barebone Mini PC Kit AMD",
            "Single Board Dev Kit 8GB",
            "Commercial Kiosk Terminal PC",
            "High-Performance Compute Node",
            "Silent Fanless Mini PC Pro",
            "Precision CAD Workstation",
            "Home Media Server Appliance",
            "Convertible 2-in-1 Tablet PC",
            "Enterprise Linux Laptop 16GB",
            "Server Blade Module Dual Xeon",
            "Portable Monitor 15.6 USB-C",
            "Mobile Workstation 17 RTX",
            "Rugged Industrial Tablet 10",
            "Network Storage Controller Unit",
            "Engineering Simulation Node",
        ],
    },
    "Accessories": {
        "prefix": "SUP-ACC",
        "base_price": 19.99,
        "price_step": 3.50,
        "items": [
            "Mechanical Keyboard RGB Linear",
            "Wireless Ergonomic Mouse Multi",
            "USB-C Multi-Port Hub 8-in-1",
            "Thunderbolt 4 Docking Station",
            "Aluminum Laptop Stand Cooling",
            "Vertical Ergonomic Wireless Mouse",
            "Desk Mat Extended Gaming XXL",
            "Braided USB-C to HDMI 4K Cable",
            "DisplayPort 1.4 Cable 2-Meter",
            "Webcam 1080p HD with Mic",
            "Laptop Privacy Screen Filter 15",
            "GaN Fast Wall Charger 100W",
            "Cable Management Raceway Kit",
            "Dual Monitor Desk Mount Arm",
            "Adjustable Aluminum Laptop Riser",
            "M.2 NVMe SSD External Enclosure",
            "Wireless Numeric Keypad Slim",
            "Rechargeable Active Stylus Pen",
            "Ergonomic Trackball Mouse 2.4G",
            "Magnetic Desk Phone Stand",
            "USB-C Extension Cable 10Gbps",
            "Desktop Ring Light with Stand",
            "Matte Anti-Glare Screen Protector",
            "Headphone Stand with USB Hub",
            "Leather Desk Pad Protector XL",
            "Heavy Duty Braided USB-C 3m",
            "Compact 65W GaN Travel Charger",
            "PBT Double-Shot Keycaps Set",
            "Multi-Card Reader USB 3.0 Pro",
            "Memory Foam Wrist Rest Set",
        ],
    },
    "Office": {
        "prefix": "SUP-OFF",
        "base_price": 15.50,
        "price_step": 2.25,
        "items": [
            "Cross-Cut Paper Shredder 12-Sheet",
            "Heavy Duty 2-Hole Punch Steel",
            "Electric Automatic Pencil Sharpener",
            "Thermal Laminator Machine 9-inch",
            "Magnetic Whiteboard 36x24 Silver",
            "High-Speed Document Scanner Duplex",
            "Direct Thermal Shipping Label Printer",
            "Heavy Duty Electric Stapler",
            "Desk Organizer Mesh 5-Tier",
            "Ergonomic Adjustable Footrest",
            "Computer Monitor Memo Board Set",
            "Dimmable LED Desk Lamp USB",
            "Rotary Paper Trimmer 12-inch",
            "Heavy Duty Steel Cash Drawer",
            "Wireless Barcode Scanner Handheld",
            "Digital Postal Shipping Scale 50kg",
            "Bluetooth Conference Speakerphone",
            "Memory Foam Ergonomic Seat Cushion",
            "Document Tray Letter Sorter 3-Tier",
            "Silent Wall Clock Non-Ticking 12in",
            "Dry Erase Magnetic Eraser Pack",
            "Retractable ID Badge Reel 5-Pack",
            "Desktop Business Calculator 12-Digit",
            "Weighted Desktop Tape Dispenser",
            "Heavy Duty Steel Bookends Pair",
            "Clamp-On Desk Power Strip 4-AC",
            "Incline Wire File Sorter 8-Slot",
            "Frameless Glass Dry Erase Board",
            "Acoustic Desk Privacy Panel 48in",
            "Under-Desk Cable Management Tray",
        ],
    },
    "Furniture": {
        "prefix": "SUP-FURN",
        "base_price": 89.00,
        "price_step": 18.50,
        "items": [
            "Ergonomic High-Back Mesh Chair",
            "Electric Dual-Motor Standing Desk",
            "Locking Mobile File Cabinet 3-Draw",
            "Executive Bonded Leather Chair",
            "Reversible L-Shaped Corner Desk",
            "Adjustable Drafting Stool with Ring",
            "Commercial Steel Bookcase 5-Shelf",
            "Active Balance Wobble Stool",
            "Dual Monitor Stand Riser Wood",
            "Under-Desk Pull-Out Keyboard Tray",
            "Office Storage Credenza 2-Door",
            "Heavy Duty Mobile CPU Cart",
            "Modular Conference Table 8-Foot",
            "Free-Standing Acoustic Room Divider",
            "Anti-Fatigue Standing Desk Mat",
            "Reception Guest Chair Fabric",
            "Mid-Back Task Chair Breathable",
            "Metal Storage Locker 2-Door",
            "Nesting Training Table Wheels",
            "Architect Drafting Table Adjustable",
            "Ergonomic Saddle Stool Rolling",
            "Modular Office Lounge Chair",
            "Steel Rolling Utility Cart 3-Tier",
            "Sideboard Storage Cabinet 4-Door",
            "Lumbar Support Cushion Breathable",
            "Articulated Cable Management Spine",
            "Rocking Ergonomic Foot Platform",
            "Heavy Duty Single Monitor Clamp",
            "Recycling Waste Bin Dual-Compartment",
            "Compact Home Work Desk 40-inch",
        ],
    },
    "Printers": {
        "prefix": "SUP-PRNT",
        "base_price": 129.00,
        "price_step": 32.00,
        "items": [
            "Color Laser Multifunction Pro",
            "High-Speed Monochrome Laser Duplex",
            "Wireless All-in-One Inkjet Multi",
            "Wide-Format Pro Photo Inkjet",
            "Commercial Thermal Shipping Printer",
            "Portable Bluetooth Receipt Printer",
            "Heavy Duty Enterprise Workgroup",
            "Dot Matrix Impact Form Printer",
            "Dual-Extruder Desktop 3D Printer",
            "High-Resolution Resin 3D Printer",
            "Industrial Barcode Label Printer",
            "Compact Photo Printer Wireless",
            "Continuous Ink Tank System Multi",
            "High-Volume Floor Digital Copier",
            "Sheetfed Duplex Document Scanner",
            "Plastic ID Card Badge Printer Dual",
            "Vinyl Cutter Plotter 28-inch",
            "Thermal Transfer Ribbon Printer",
            "Eco-Tank Mono Multifunction Unit",
            "Pocket Mini Photo Printer Instant",
            "Architectural Plotter 36-inch CAD",
            "Automatic Document Feeder Copier",
            "Color Label Roll Printer Inkjet",
            "Rugged Mobile Receipt Terminal 3in",
            "Wireless Compact Document Printer",
            "Flatbed UV Printing Machine",
            "3D Filament Dryer Storage Box",
            "Kiosk Embedded Thermal Receipt Unit",
            "Departmental Network Multi-Laser",
            "Mobile Wireless Label Maker QWERTY",
        ],
    },
    "Networking": {
        "prefix": "SUP-NET",
        "base_price": 39.99,
        "price_step": 15.00,
        "items": [
            "Managed Gigabit Switch 24-Port L2",
            "Wi-Fi 7 Tri-Band Enterprise Router",
            "Ceiling PoE Access Point AX3000",
            "Hardware Firewall VPN Router",
            "10GbE SFP+ Aggregation Switch 8P",
            "Gigabit PoE+ Unmanaged Switch 8-Port",
            "Mesh Wi-Fi 6 System 3-Pack Whole",
            "Cat6A Shielded Patch Cable 100ft",
            "Outdoor High-Power Wireless AP",
            "Fiber Gigabit Media Converter Pair",
            "Cat6 48-Port 1U Rackmount Patch",
            "Enterprise Core Managed Switch 48P",
            "Industrial DIN-Rail PoE Switch",
            "Long-Range Ceiling Mount Wi-Fi AP",
            "Multi-WAN Gigabit VPN Gateway",
            "Wall-Mount Network Cabinet 9U Glass",
            "10GBASE-SR SFP+ Optical Transceiver",
            "Gigabit Powerline Ethernet Starter",
            "Dual-Band AC1300 USB Wi-Fi Adapter",
            "PoE+ Injector 30W Gigabit 802.3at",
            "Industrial 4G LTE Cellular Gateway",
            "Cloud-Managed Security Appliance",
            "Desktop Ethernet Switch 16-Port",
            "Digital Network Cable Wire Tracer",
            "RJ45 Pass-Through Crimper Tool Kit",
            "Omni-Directional Dual-Band Antenna",
            "Layer 3 Border Gateway Router",
            "Managed Reverse PoE Switch 16P",
            "Multimode Duplex LC-LC Fiber 10m",
            "Cat6 Keystone Jacks 90-Degree 24pk",
        ],
    },
    "Storage": {
        "prefix": "SUP-STOR",
        "base_price": 45.00,
        "price_step": 14.50,
        "items": [
            "Enterprise NAS 4-Bay Tower 2.5G",
            "Rugged Portable SSD 2TB IP67",
            "PCIe 4.0 NVMe M.2 SSD 4TB Heatsink",
            "Desktop External Hard Drive 12TB",
            "Enterprise SAS Hard Drive 16TB 7.2K",
            "High-Endurance MicroSDXC 256GB V30",
            "Hardware-Encrypted USB 3.0 128GB",
            "Rackmount NAS Server 8-Bay 2U",
            "Ultra-Slim Portable SSD 1TB USB 3.2",
            "Enterprise 2.5-inch SATA SSD 3.84TB",
            "Hardware RAID Enclosure Dual-Bay",
            "CFexpress Type B Memory Card 512GB",
            "Surveillance Internal HDD 8TB 24/7",
            "Portable External Hard Drive 5TB",
            "Thunderbolt 3 NVMe Mobile Drive 2TB",
            "Dual-Slot SATA HDD Docking Station",
            "LTO-8 Ultrium Data Tape 12TB/30TB",
            "PCIe Gen5 x4 M.2 SSD 2TB Extreme",
            "Pro Grade MicroSD Card 512GB A2",
            "USB 3.2 Dual Drive OTG Type-C 256GB",
            "Compact 2-Bay Home Cloud NAS Server",
            "All-Terrain Rugged External HDD 4TB",
            "Enterprise High-Density NVMe 7.68TB",
            "Portable SSD USB-C 500GB Aluminium",
            "Dash Cam Continuous MicroSD 128GB",
            "FIPS 140-2 Level 3 Secure Key 64GB",
            "Enterprise U.2 NVMe SSD 3.2TB PCIe",
            "Archival Optical Disc BDXL 100GB 5pk",
            "Internal Solid State Drive 1TB TLC",
            "Metal Mini USB 3.0 Flash Drive 128GB",
        ],
    },
}


def build_sql_script() -> tuple[str, list[dict]]:
    """Build the SQL creation script and return rows list."""
    rows: list[dict] = []
    global_idx = 1

    for cat_name, cat_info in CATEGORIES_DATA.items():
        prefix = cat_info["prefix"]
        base_p = cat_info["base_price"]
        step_p = cat_info["price_step"]
        for i, item_name in enumerate(cat_info["items"]):
            pid = f"PRD-{global_idx:04d}"
            supp_suffix = "01" if i < 15 else "02"
            supp_id = f"{prefix}-{supp_suffix}"
            price = round(base_p + (i * step_p), 2)
            rows.append(
                {
                    "product_id": pid,
                    "product_name": item_name,
                    "category": cat_name,
                    "supplier_id": supp_id,
                    "price": price,
                    "status": "ACTIVE",
                    "updated_at": "2026-09-01 00:00:00+00",
                }
            )
            global_idx += 1

    sql_parts = [
        "-- SCD2 Copilot Product Master DDL & Seed Script",
        "-- Deterministically seeds 240 products across 8 categories (30 each)",
        "",
        "CREATE TABLE IF NOT EXISTS public.product_master (",
        "    product_id TEXT PRIMARY KEY,",
        "    product_name TEXT NOT NULL,",
        "    category TEXT NOT NULL,",
        "    supplier_id TEXT NOT NULL,",
        "    price NUMERIC(12,2) NOT NULL CHECK (price >= 0),",
        "    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE', 'DISCONTINUED')),",
        "    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        ");",
        "",
        "CREATE INDEX IF NOT EXISTS idx_product_master_updated_at",
        "    ON public.product_master (updated_at, product_id);",
        "",
        "-- Drop update trigger temporarily to preserve historical seed timestamps",
        "DROP TRIGGER IF EXISTS trg_product_master_updated_at ON public.product_master;",
        "",
        "INSERT INTO public.product_master (product_id, product_name, category, supplier_id, price, status, updated_at)",
        "VALUES",
    ]

    val_lines = []
    for r in rows:
        name_esc = r["product_name"].replace("'", "''")
        val_lines.append(
            f"    ('{r['product_id']}', '{name_esc}', '{r['category']}', '{r['supplier_id']}', {r['price']:.2f}, '{r['status']}', '{r['updated_at']}')"
        )
    sql_parts.append(",\n".join(val_lines))
    sql_parts.extend(
        [
            "ON CONFLICT (product_id) DO UPDATE SET",
            "    product_name = EXCLUDED.product_name,",
            "    category = EXCLUDED.category,",
            "    supplier_id = EXCLUDED.supplier_id,",
            "    price = EXCLUDED.price,",
            "    status = EXCLUDED.status,",
            "    updated_at = EXCLUDED.updated_at;",
            "",
            "-- Create automatic timestamp update trigger function",
            "CREATE OR REPLACE FUNCTION update_product_master_timestamp()",
            "RETURNS TRIGGER AS $$",
            "BEGIN",
            "    NEW.updated_at = NOW();",
            "    RETURN NEW;",
            "END;",
            "$$ LANGUAGE plpgsql;",
            "",
            "CREATE TRIGGER trg_product_master_updated_at",
            "    BEFORE UPDATE ON public.product_master",
            "    FOR EACH ROW",
            "    EXECUTE FUNCTION update_product_master_timestamp();",
            "",
        ]
    )

    return "\n".join(sql_parts), rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Product Master in PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Generate SQL only without executing")
    args = parser.parse_args()

    sql_content, rows = build_sql_script()
    sql_file = Path("sql/seed_product_master.sql")
    sql_file.parent.mkdir(parents=True, exist_ok=True)
    sql_file.write_text(sql_content, encoding="utf-8")
    logger.info("Saved SQL script with %d records to %s", len(rows), sql_file)

    if args.dry_run:
        logger.info("Dry-run requested. Exiting without database execution.")
        return 0

    settings = get_settings()
    db = DatabaseManager(settings=settings)
    if not db.is_configured:
        logger.error("DATABASE_URL is not configured. Cannot seed database.")
        return 1

    logger.info("Connecting to PostgreSQL to apply DDL and seed 240 products...")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_content)
        conn.commit()

        # Validate count
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total_count, COUNT(DISTINCT category) AS category_count FROM public.product_master;")
            res = cur.fetchone()
            if isinstance(res, dict):
                total_count = res.get("total_count", 0)
                category_count = res.get("category_count", 0)
            elif res:
                total_count, category_count = res[0], res[1]
            else:
                total_count, category_count = 0, 0
            logger.info(
                "Verification SUCCESS: public.product_master contains %d rows across %d categories.",
                total_count,
                category_count,
            )
            assert total_count == 240, f"Expected 240 products, found {total_count}"
            assert category_count == 8, f"Expected 8 categories, found {category_count}"

    logger.info("Product Master seeding completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
