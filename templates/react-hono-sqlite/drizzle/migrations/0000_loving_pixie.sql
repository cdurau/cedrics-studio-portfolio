CREATE TABLE `entries` (
	`id` integer PRIMARY KEY NOT NULL,
	`content` text NOT NULL,
	`created_at` integer DEFAULT (cast((julianday('now') - 2440587.5)*86400000 as integer)) NOT NULL
);
