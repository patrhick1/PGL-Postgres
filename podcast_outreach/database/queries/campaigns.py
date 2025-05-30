"""Campaign related database queries."""
from typing import Dict, Any, Optional, List, Tuple
import uuid
import json
from datetime import date, datetime, timezone # Ensure timezone is imported

from podcast_outreach.logging_config import get_logger
from podcast_outreach.database.connection import get_db_pool

logger = get_logger(__name__)

async def create_campaign_in_db(campaign_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    query = """
    INSERT INTO campaigns (
        campaign_id, person_id, attio_client_id, campaign_name, campaign_type,
        campaign_bio, campaign_angles, campaign_keywords, compiled_social_posts,
        podcast_transcript_link, compiled_articles_link, mock_interview_trancript,
        start_date, end_date, goal_note, media_kit_url, instantly_campaign_id,
        questionnaire_responses -- <<< NEW FIELD
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
        $18 -- <<< NEW PARAMETER
    ) RETURNING *;
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            keywords = campaign_data.get("campaign_keywords", []) or []
            # For JSONB, asyncpg can take a Python dict directly.
            # If campaign_data['questionnaire_responses'] is already a dict, it's fine.
            # If it might be a JSON string, you'd parse it: json.loads(campaign_data.get('questionnaire_responses'))
            q_responses = campaign_data.get('questionnaire_responses') # Should be a dict or None

            row = await conn.fetchrow(
                query,
                campaign_data['campaign_id'], campaign_data['person_id'], campaign_data.get('attio_client_id'),
                campaign_data['campaign_name'], campaign_data.get('campaign_type'),
                campaign_data.get('campaign_bio'), campaign_data.get('campaign_angles'), keywords,
                campaign_data.get('compiled_social_posts'), campaign_data.get('podcast_transcript_link'),
                campaign_data.get('compiled_articles_link'), campaign_data.get('mock_interview_trancript'),
                campaign_data.get('start_date'), campaign_data.get('end_date'),
                campaign_data.get('goal_note'), campaign_data.get('media_kit_url'),
                campaign_data.get('instantly_campaign_id'),
                q_responses # <<< NEW VALUE
            )
            logger.info(f"Campaign created: {campaign_data.get('campaign_id')}")
            return dict(row) if row else None
        except Exception as e:
            logger.exception(f"Error creating campaign (ID: {campaign_data.get('campaign_id')}) in DB: {e}")
            raise

async def get_campaign_by_id(campaign_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM campaigns WHERE campaign_id = $1;"
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(query, campaign_id)
            if not row:
                logger.warning(f"Campaign not found: {campaign_id}")
                return None
            return dict(row)
        except Exception as e:
            logger.exception(f"Error fetching campaign {campaign_id}: {e}")
            raise

async def get_all_campaigns_from_db(skip: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
    query = "SELECT * FROM campaigns ORDER BY created_at DESC OFFSET $1 LIMIT $2;"
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(query, skip, limit)
            return [dict(row) for row in rows]
        except Exception as e:
            logger.exception(f"Error fetching all campaigns: {e}")
            raise

async def update_campaign(campaign_id: uuid.UUID, update_fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not update_fields:
        logger.warning(f"No update data for campaign {campaign_id}. Fetching current.")
        return await get_campaign_by_id(campaign_id)

    set_clauses = []
    values = []
    idx = 1
    for key, val in update_fields.items():
        if key == "campaign_keywords" and val is not None and not isinstance(val, list):
            keywords_list = [kw.strip() for kw in str(val).split(',') if kw.strip()]
            if not keywords_list and str(val).strip():
                keywords_list = [kw.strip() for kw in str(val).split() if kw.strip()]
            val = keywords_list
        
        # For JSONB fields, asyncpg handles Python dicts directly.
        # If 'val' for 'questionnaire_responses' is a dict, it's fine.
        # If it's a JSON string, you might need json.loads(val) if asyncpg doesn't auto-cast.
        # However, Pydantic model should ensure it's a dict if coming from API.
        if key == "questionnaire_responses" and isinstance(val, str):
            try:
                val = json.loads(val) # Ensure it's a dict if it came as a string
            except json.JSONDecodeError:
                logger.warning(f"Could not parse questionnaire_responses string for campaign {campaign_id}, storing as NULL or original if not string.")
                val = None # Or handle error appropriately

        set_clauses.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    if not set_clauses:
        return await get_campaign_by_id(campaign_id)

    set_clause_str = ", ".join(set_clauses)
    query = f"UPDATE campaigns SET {set_clause_str} WHERE campaign_id = ${idx} RETURNING *;"
    values.append(campaign_id)

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(query, *values)
            logger.info(f"Campaign updated: {campaign_id} with fields: {list(update_fields.keys())}")
            return dict(row) if row else None
        except Exception as e:
            logger.exception(f"Error updating campaign {campaign_id}: {e}")
            raise

async def delete_campaign_from_db(campaign_id: uuid.UUID) -> bool:
    query = "DELETE FROM campaigns WHERE campaign_id = $1;"
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            result = await conn.execute(query, campaign_id)
            deleted_count = int(result.split(" ")[1]) if result.startswith("DELETE ") else 0
            if deleted_count > 0:
                logger.info(f"Campaign deleted: {campaign_id}")
                return True
            logger.warning(f"Campaign not found for deletion or delete failed: {campaign_id}")
            return False
        except Exception as e:
            logger.exception(f"Error deleting campaign {campaign_id} from DB: {e}")
            raise

async def count_active_campaigns(person_id: Optional[int] = None) -> int:
    """Counts active campaigns. An active campaign has no end_date or end_date is in the future."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            today = date.today()
            base_query = "SELECT COUNT(*) FROM campaigns WHERE (end_date IS NULL OR end_date >= $1)"
            params = [today]
            if person_id is not None:
                base_query += f" AND person_id = ${len(params) + 1}"
                params.append(person_id)
            
            count = await conn.fetchval(base_query, *params)
            return count if count is not None else 0
        except Exception as e:
            logger.exception(f"Error counting active campaigns (person_id: {person_id}): {e}")
            return 0

async def get_campaigns_with_embeddings(limit: int = 200, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """
    Fetches campaigns that have an embedding, ordered by creation date.
    Returns a list of campaign records and the total count of such campaigns.
    """
    query = """
    SELECT *
    FROM campaigns
    WHERE embedding IS NOT NULL
    ORDER BY created_at DESC
    LIMIT $1 OFFSET $2;
    """
    count_query = "SELECT COUNT(*) FROM campaigns WHERE embedding IS NOT NULL;"
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(query, limit, offset)
            total_count_record = await conn.fetchrow(count_query)
            total = total_count_record['count'] if total_count_record else 0
            return [dict(row) for row in rows], total
        except Exception as e:
            logger.error(f"Error fetching campaigns with embeddings: {e}", exc_info=True)
            return [], 0

async def update_campaign_status(campaign_id: uuid.UUID, status: str, status_message: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Updates the status of a campaign. Assumes 'status' and 'status_message' columns exist or are handled by other means.
    This is a placeholder; actual implementation depends on how campaign status is stored.
    If campaigns table has 'campaign_status' and 'status_notes' fields, it would be:
    """
    # Example: UPDATE campaigns SET campaign_status = $1, status_notes = $2, updated_at = NOW() 
    #          WHERE campaign_id = $3 RETURNING *;
    logger.info(f"Attempting to update status for campaign {campaign_id} to '{status}' with message: '{status_message}'. This function is a placeholder.")
    # For now, just fetch and return the campaign as no actual status fields are defined for update here.
    return await get_campaign_by_id(campaign_id)