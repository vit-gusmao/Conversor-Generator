import { Injectable } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';

/**
 * Strict guard (passport-jwt). Not attached to any current public route —
 * `OptionalJwtGuard` is the contract default — but exported so future
 * admin-only endpoints can require a real token regardless of AUTH_MODE.
 */
@Injectable()
export class JwtAuthGuard extends AuthGuard('jwt') {}
