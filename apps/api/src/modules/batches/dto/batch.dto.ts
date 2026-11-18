import { Type } from 'class-transformer';
import {
  IsIn,
  IsInt,
  IsObject,
  IsOptional,
  IsString,
  Max,
  MaxLength,
  Min,
  MinLength,
} from 'class-validator';

export class CreateBatchDto {
  @IsString({ message: 'client_batch_key deve ser um texto' })
  @MinLength(1, { message: 'client_batch_key é obrigatório' })
  @MaxLength(128, { message: 'client_batch_key deve ter no máximo 128 caracteres' })
  client_batch_key!: string;
}

export class RegisterBatchItemDto {
  @IsString({ message: 'client_item_id deve ser um texto' })
  @MinLength(1, { message: 'client_item_id é obrigatório' })
  @MaxLength(128, { message: 'client_item_id deve ter no máximo 128 caracteres' })
  client_item_id!: string;

  @IsString({ message: 'filename deve ser um texto' })
  @MinLength(1, { message: 'filename é obrigatório' })
  @MaxLength(512, { message: 'filename deve ter no máximo 512 caracteres' })
  filename!: string;

  @Type(() => Number)
  @IsInt({ message: 'size_bytes deve ser um número inteiro' })
  @Min(1, { message: 'size_bytes deve ser maior que zero' })
  size_bytes!: number;

  @IsOptional()
  @IsObject({ message: 'options deve ser um objeto' })
  options?: Record<string, unknown> | null;
}

export class ListBatchItemsQueryDto {
  @IsOptional()
  @IsString({ message: 'cursor deve ser um texto' })
  @MaxLength(512)
  cursor?: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'limit deve ser um número inteiro' })
  @Min(1, { message: 'limit deve ser maior ou igual a 1' })
  @Max(200, { message: 'limit deve ser no máximo 200' })
  limit?: number;

  @IsOptional()
  @IsString({ message: 'status deve ser um texto' })
  @MaxLength(32)
  status?: string;
}

export class RetryBatchItemDto {
  @IsOptional()
  @IsIn(['upload', 'conversion'], { message: "scope deve ser 'upload' ou 'conversion'" })
  scope?: 'upload' | 'conversion';
}
