-- Add is_manual column to artwork_translations to distinguish author-uploaded from AI-generated
ALTER TABLE artwork_translations ADD COLUMN IF NOT EXISTS is_manual BOOLEAN NOT NULL DEFAULT FALSE;

-- Add image_index for per-image translation support in multi-image artworks
ALTER TABLE artwork_translations ADD COLUMN IF NOT EXISTS image_index INTEGER NOT NULL DEFAULT 0;

-- Drop old unique constraint and create new one including image_index
-- (constraint name may vary; run both to be safe)
DO $$ BEGIN
  ALTER TABLE artwork_translations DROP CONSTRAINT IF EXISTS uid_artwork_tr_artwork__b0523f;
  ALTER TABLE artwork_translations DROP CONSTRAINT IF EXISTS artwork_translations_artwork_id_target_lang_key;
EXCEPTION WHEN others THEN NULL;
END $$;
ALTER TABLE artwork_translations
  ADD CONSTRAINT artwork_translations_artwork_target_lang_image_idx_unique
  UNIQUE (artwork_id, target_lang, image_index);
